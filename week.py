"""This Week: what to do in a given week, per class, worked out from Canvas's modules.

See WEEK.md for the decisions behind this. The short version: a Canvas check already
stores a snapshot per course, and now the course's modules ride along in it. Nothing
here writes a class row. The weeks are computed from the snapshot on every request, as
the Canvas review is, so there is no stored plan to go stale; the only thing stored is
what he ticked (`week_marks`).

Everything above the database section is pure and is tested against his real Fall 2026
modules, pinned in tests/fixtures/modules_fall2026.json.
"""
import re
from collections import Counter
from datetime import date, timedelta

import canvas


# ---------------------------------------------------------------------------
# reducing modules for the snapshot
# ---------------------------------------------------------------------------

MODULE_FIELDS = ("id", "name", "position", "unlock_at", "state")
ITEM_FIELDS = ("id", "title", "type", "content_id", "html_url", "external_url",
               "completion_requirement", "page_url")
DETAIL_FIELDS = ("due_at", "points_possible")


def reduce_modules(modules):
    """Only what the week needs, so a course's snapshot row stays small."""
    out = []
    for m in modules or []:
        mm = {k: m.get(k) for k in MODULE_FIELDS}
        items = []
        for it in m.get("items") or []:
            ii = {k: it.get(k) for k in ITEM_FIELDS if it.get(k) is not None}
            if "title" in ii:
                ii["title"] = canvas._pg_safe(ii["title"])
            details = it.get("content_details") or {}
            kept = {k: details[k] for k in DETAIL_FIELDS if details.get(k) is not None}
            if kept:
                ii["content_details"] = kept
            items.append(ii)
        mm["items"] = items
        out.append(mm)
    return out


# ---------------------------------------------------------------------------
# weeks and dates
# ---------------------------------------------------------------------------

# "Week 01", "Week 2: ...", "MODULE 1, WEEK 3", " Week 6". Not "Weekly", and not a
# week written as a word, which none of his courses do.
WEEK_RE = re.compile(r"\bweek\s*0*(\d{1,2})\b", re.I)

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
          "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
# "Sep. 22", "September 29th", and the other way round, "19 Oct".
DATE_MD = re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
DATE_DM = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"(?![a-z])", re.I)


def monday_of(d):
    return d - timedelta(days=d.weekday())


def parse_iso(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def week_number(d, term_start):
    """Week 1 is the Monday-to-Sunday week holding the term's first day.

    The same convention as `syllabus.resolve_week`, so a syllabus's "Week 6" and a
    module's "Week 6" land on the same dates unless the course numbers its own weeks
    differently, which is what the offset below is for.
    """
    return (monday_of(d) - monday_of(term_start)).days // 7 + 1


def week_start(n, term_start):
    return monday_of(term_start) + timedelta(weeks=n - 1)


def week_in(text):
    m = WEEK_RE.search(text or "")
    return int(m.group(1)) if m else None


def dates_in(text, term_start):
    """Calendar dates written in a line of text, in the term's year.

    A date more than two months before the term starts is taken to be next year, which
    only matters for a term that crosses New Year. SFU's do not, but a friend's might.
    """
    found = []
    raw = text or ""
    pairs = [(m.group(1), m.group(2)) for m in DATE_MD.finditer(raw)]
    pairs += [(m.group(2), m.group(1)) for m in DATE_DM.finditer(raw)]
    for mon, day in pairs:
        month = MONTHS.get(mon.lower()[:3])
        try:
            d = date(term_start.year, month, int(day))
        except (TypeError, ValueError):
            continue
        if d < term_start - timedelta(days=60):
            d = date(d.year + 1, d.month, d.day)
        found.append(d)
    return found


# ---------------------------------------------------------------------------
# splitting modules into weeks
# ---------------------------------------------------------------------------

# SubHeaders that only say what kind of thing follows. They decide checkbox or link,
# and are not worth showing as a note on their own.
PLAIN_SECTIONS = {"readings", "reading", "assignments", "assignment", "lecture slides",
                  "slides", "lectures", "lecture", "tutorial assignments", "tutorials",
                  "core resources", "resources", "recommended readings", "videos",
                  "activities", "to do"}


def _topic(name):
    """What a week module is about, with its "Week N" taken out.

    "Week 2: Our Changing Relationship with the Written Word" -> "Our Changing
    Relationship with the Written Word"; "MODULE 1, WEEK 3" -> "Module 1".
    """
    text = WEEK_RE.sub("", name or "")
    text = re.sub(r"^[\s,:;\-–—]+|[\s,:;\-–—]+$", "", text)
    text = re.sub(r"\s*[,:;]\s*[,:;]\s*", ": ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if text.isupper():
        text = text.capitalize()
    return text


def split_weeks(modules):
    """The course's modules as {week number: {"topic", "notes", "items"}}.

    Three shapes, all from his real courses:

    - a module per week, "Week 01" (REM 388), "Week 2: ..." (PSYC 300W),
      "MODULE 1, WEEK 3" (SD 381): the whole module is that week;
    - a themed module with "Week N" SubHeaders inside (IAT 201): each SubHeader starts
      a week, until the next one;
    - a module with no week in it (course overview, resources, PHIL 110's "Slides"):
      not part of any week, except an item whose own title starts "Week N", which goes
      to that week. PHIL 110's decks are named "Week 1 - What is logic.pdf".

    Each item carries the SubHeader it sits under as `section`, which is how PSYC
    300W's READINGS are told apart from its LECTURE SLIDES.
    """
    weeks = {}

    def week(n):
        return weeks.setdefault(n, {"topic": "", "notes": [], "items": []})

    for m in modules or []:
        name = m.get("name") or ""
        module_week = week_in(name)
        if module_week:
            w = week(module_week)
            w["topic"] = w["topic"] or _topic(name)
            w["dates"] = w.get("dates", []) + [name]
        current, section = module_week, ""
        for it in m.get("items") or []:
            title = it.get("title") or ""
            if it.get("type") == "SubHeader":
                sub_week = None if module_week else week_in(title)
                if sub_week:
                    current, section = sub_week, ""
                    w = week(current)
                    w["topic"] = w["topic"] or _topic(name)
                    continue
                section = title.strip()
                if current and canvas_norm(section) not in PLAIN_SECTIONS:
                    w = week(current)
                    w["notes"].append(section)
                    w["dates"] = w.get("dates", []) + [section]
                continue
            if current:
                week(current)["items"].append(dict(it, section=section, module=name))
                continue
            own = week_in(title) if re.match(r"\s*week\b", title, re.I) else None
            if own:
                week(own)["items"].append(dict(it, section=section, module=name,
                                               forceLink=True))
    return weeks


def canvas_norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# ---------------------------------------------------------------------------
# checkbox or link
# ---------------------------------------------------------------------------

VIDEO_RE = re.compile(r"\.(mp4|mov|m4v|webm)\b|\bvideo\b|\blecture recording\b", re.I)
VIDEO_HOSTS = ("vimeo.com", "youtube.com", "youtu.be", "mediasite")
READ_RE = re.compile(r"^\s*read\b", re.I)
READ_SECTION = re.compile(r"\breading", re.I)
LINK_SECTION = re.compile(r"slide|handout|resource|recommended|optional|supplement", re.I)
LINK_TITLE = re.compile(r"slides?\b|\.key\.pdf|\.pptx?\b|handout|\btut\b|tutorial|worksheet|"
                        r"appendix|template|syllabus|course outline|\bsummary\b", re.I)
WORK_PAGE = re.compile(r"lecture|activity|preparation|material|lesson", re.I)
EXAM_RE = re.compile(r"\b(midterm|mid-term|final exam|exam)\b", re.I)
EXTENSION_RE = re.compile(r"\.(pdf|docx?|pptx?|key|mp4|mov|m4v|txt|rtf)$", re.I)


# "MODULE 1, WEEK 1- ", "Week 1 - ": already said by the week the row is shown under.
WEEK_PREFIX = re.compile(r"^\s*(module\s*\d+\s*[,:\-–]?\s*)?(week\s*\d+\s*[,:\-–]?\s*)?", re.I)


def display_title(title):
    """A file's name without its extension, and without a leading "Module 1, Week 1"."""
    text = (title or "").strip()
    text = re.sub(r"\.key\.pdf$", "", text, flags=re.I)
    text = EXTENSION_RE.sub("", text).strip()
    rest = WEEK_PREFIX.sub("", text).strip()
    return rest or text or (title or "").strip()


def classify(item):
    """(kind, is_task) for one module item.

    kind is one of reading, watch, quiz, exam, discussion, assignment, work, link. The
    rules are the ones in WEEK.md, and each exists for a real item:

    - Assignment, Quiz, Discussion are always work. IAT 201's readings are Canvas
      assignments named "Read Noba ...", so an assignment starting "Read" is a reading.
    - A lecture video is work: "IAT201.1_Fall2026.mp4", REM 388's "Lecture 03 - Video
      01" pages, a Vimeo link.
    - A file or link is a reading under a READINGS SubHeader (PSYC 300W) or when its
      title starts "Read" (IAT 201's "Read Rethinking HCI Education 2026.pdf").
    - Slides, handouts and tutorial PDFs are links, which is most of REM 388's week.
    - A page is work when it is a lecture or activity (SD 381, an online course, is
      entirely pages), and a link otherwise (IAT 201's "DDA1: Resources").
    """
    kind = item.get("type") or ""
    title = item.get("title") or ""
    section = item.get("section") or ""
    url = (item.get("external_url") or "").lower()
    if item.get("forceLink") or kind in ("ExternalTool", "SubHeader"):
        return "link", False
    if kind == "Quiz":
        return ("exam" if EXAM_RE.search(title) else "quiz"), True
    if kind == "Discussion":
        return "discussion", True
    if kind == "Assignment":
        if READ_RE.search(title):
            return "reading", True
        if EXAM_RE.search(title):
            return "exam", True
        if re.search(r"\bquiz\b", title, re.I):
            return "quiz", True
        return "assignment", True
    if VIDEO_RE.search(title) or any(h in url for h in VIDEO_HOSTS):
        return "watch", True
    if LINK_SECTION.search(section) or LINK_TITLE.search(title):
        return "link", False
    if READ_SECTION.search(section) or READ_RE.search(title):
        return "reading", True
    if kind == "Page" and WORK_PAGE.search(title):
        return "work", True
    return "link", False


# ---------------------------------------------------------------------------
# a course's own week numbering
# ---------------------------------------------------------------------------

MAX_OFFSET = 1
FORWARD_RE = re.compile(r"\bdue\b|\bupcoming\b|\bnext\b", re.I)


def _mode(votes):
    """The most common vote; a tie goes to the one nearest zero, then the smaller."""
    if not votes:
        return None
    counts = Counter(votes)
    best = max(counts.values())
    return sorted((v for v, c in counts.items() if c == best), key=lambda v: (abs(v), v))[0]


def course_offset(weeks, term_start, tz=None):
    """How far a course's "Week N" sits from the calendar's week N, as (offset, source).

    Courses do not agree on what Week 1 is. PSYC 300W's first class is a Tuesday in
    the second calendar week, so its "Week 2" readings are "do before Sep. 22 class",
    which is calendar week 3. REM 388's "Week 07 - Midterm - Monday, 19 Oct" is
    calendar week 7.

    Dates written in a week's module name and SubHeaders are the evidence, because
    they say when that week happens. Due dates are only a fallback: they are
    deliberately not the week's own dates (PSYC 300W's Week 3 module holds a tutorial
    assignment due in calendar week 3, before that week's class), so they vote only
    when nothing is written down. With neither, zero.
    """
    text_votes, due_votes = [], []
    for n, w in weeks.items():
        for line in w.get("dates", []):
            # "Learn about an upcoming video assignment due Oct. 4th" sits in SD 381's
            # Week 2 and is about week 4: a date for something else, not for the week.
            if FORWARD_RE.search(line):
                continue
            for d in dates_in(line, term_start):
                text_votes.append(week_number(d, term_start) - n)
        for it in w["items"]:
            due = (it.get("content_details") or {}).get("due_at")
            day = parse_iso(canvas.due_local(due, tz)[0]) if due else None
            if day:
                due_votes.append(week_number(day, term_start) - n)
    # No course numbers its weeks more than one away from the calendar's. A vote
    # further out is a date about something else, and letting it win would move a
    # whole course's readings a fortnight.
    text_votes = [v for v in text_votes if abs(v) <= MAX_OFFSET]
    due_votes = [v for v in due_votes if abs(v) <= MAX_OFFSET]
    if text_votes:
        return _mode(text_votes), "written"
    if due_votes:
        return _mode(due_votes), "due"
    return 0, "none"


# ---------------------------------------------------------------------------
# a course's entries, placed in calendar weeks
# ---------------------------------------------------------------------------

def entry_key(course_id, item):
    return "canvas:%s:mi:%s" % (course_id, item.get("id"))


def course_entries(course_id, modules, term_start, tz=None, shift=None):
    """Every week entry for one course, each placed in a calendar week.

    Returns {"offset", "offsetSource", "weeks": {calendar week: {"topic", "notes"}},
    "entries": [...]}. An item with a due date goes in the week it is due, whatever
    module it sits in, because a to-do list is about when things are due; an undated
    one follows its module's week, shifted by the course's offset. `shift`, set by
    hand in the interface, replaces the worked-out offset.
    """
    weeks = split_weeks(modules)
    offset, source = course_offset(weeks, term_start, tz)
    if shift is not None:
        offset, source = int(shift), "set"
    out_weeks, entries, seen = {}, [], set()
    for n in sorted(weeks):
        w = weeks[n]
        cal = n + offset
        if w["topic"] or w["notes"]:
            slot = out_weeks.setdefault(cal, {"topic": "", "notes": []})
            slot["topic"] = slot["topic"] or w["topic"]
            slot["notes"] += [x for x in w["notes"] if x not in slot["notes"]]
        for it in w["items"]:
            # The same file linked from two places in one week is one row.
            ident = (it.get("type"), it.get("content_id") or it.get("external_url") or it.get("title"))
            if (cal, ident) in seen:
                continue
            seen.add((cal, ident))
            kind, task = classify(it)
            due_date, due_time = canvas.due_local(
                (it.get("content_details") or {}).get("due_at"), tz)
            day = parse_iso(due_date)
            req = it.get("completion_requirement") or {}
            entries.append({
                "key": entry_key(course_id, it),
                "week": week_number(day, term_start) if day else cal,
                "moduleWeek": n,
                "title": display_title(it.get("title")),
                # Canvas's own spelling, for matching to an assignment he already has.
                # The display title drops "MODULE 1, Week 1," and the assignment
                # keeps it.
                "rawTitle": (it.get("title") or "").strip(),
                "kind": kind,
                "task": task,
                "canvasType": it.get("type"),
                "contentId": it.get("content_id"),
                "url": it.get("html_url") or it.get("external_url"),
                "dueDate": due_date,
                "dueTime": due_time,
                "section": it.get("section") or "",
                "canvasDone": bool(req.get("completed")),
            })
    return {"offset": offset, "offsetSource": source, "weeks": out_weeks,
            "entries": entries}


# ---------------------------------------------------------------------------
# START cues
# ---------------------------------------------------------------------------

# Ten days, the lead IAT 201's map gives: "your cue to begin something, about ten days
# before it is due".
CUE_LEAD_DAYS = 10
# What counts as big enough to need a start date of its own.
CUE_MIN_WEIGHT = 10.0
STUDY_TYPES = ("exam", "quiz")


def cue_for(item, weight):
    """A "start this now" row for an assignment, or None when it does not need one.

    Exams always get one; anything else when it is worth at least CUE_MIN_WEIGHT
    percent. `weight` is the item's effective share of the final grade, which for an
    item in a grade category is the category's weight shared among its items.
    """
    due = parse_iso(item.get("due_date"))
    if not due or item.get("status") == "done":
        return None
    kind = (item.get("type") or "").lower()
    is_exam = kind == "exam" or bool(EXAM_RE.search(item.get("title") or ""))
    if not is_exam and (weight or 0) < CUE_MIN_WEIGHT:
        return None
    verb = "Start studying for" if is_exam or kind in STUDY_TYPES else "Start"
    return {
        "key": "cue:%s" % item["id"],
        "date": (due - timedelta(days=CUE_LEAD_DAYS)).isoformat(),
        "title": "%s %s" % (verb, (item.get("title") or "").strip()),
        "kind": "start",
        "dueDate": item.get("due_date"),
        "dueTime": item.get("due_time"),
        "itemId": item["id"],
        "weight": weight,
    }


def effective_weights(items, categories):
    """Each item's share of the final grade, as a percent, or None when unknown."""
    per_cat = Counter(i.get("category_id") for i in items if i.get("category_id"))
    cat_weight = {c["id"]: c.get("weight") for c in categories}
    out = {}
    for i in items:
        if i.get("weight") is not None:
            out[i["id"]] = float(i["weight"])
        elif i.get("category_id") and cat_weight.get(i["category_id"]) is not None:
            out[i["id"]] = float(cat_weight[i["category_id"]]) / max(1, per_cat[i["category_id"]])
        else:
            out[i["id"]] = None
    return out


# ---------------------------------------------------------------------------
# assembling a week
# ---------------------------------------------------------------------------

def _row(r):
    return dict(r) if r is not None else None


# Words that say nothing about which reading or assignment a title is.
_MATCH_STOP = {"the", "a", "an", "and", "or", "of", "in", "on", "for", "to", "with",
               "ch", "chapter", "chapters", "part", "your", "due", "cognition", "designers",
               "read", "watch"}


def _words(title):
    return {w for w in canvas_norm(title).split() if w not in _MATCH_STOP and (len(w) > 2 or w.isdigit())}


def title_match(a, b):
    """How much of the shorter title's words the longer one has, 0 to 1.

    Loose on purpose: IAT 201's map says "Read Noba: Sensation and Perception, and
    Vision" and its Canvas assignment says "Read Noba Sensation & Perception and Vision
    in Cognition for Designers". A number in both that disagrees ("Quiz 1" against
    "Quiz 2") is never a match.
    """
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    na = {w for w in wa if w.isdigit()}
    nb = {w for w in wb if w.isdigit()}
    if na and nb and not (na & nb):
        return 0.0
    short, long_ = sorted((wa, wb), key=len)
    return len(short & long_) / float(len(short))


MATCH_AT = 0.6


def assemble_class(cls, term_start, week_no, today_week, *, course=None, plan=None,
                   items=(), categories=(), materials=None, marks=None, plan_items=None):
    """One class's rows for calendar week `week_no`. Pure: everything is passed in.

    `course` is `course_entries` for the class's Canvas course, or None for a class
    that is not connected, which still gets its assignments and cues. `plan` is the
    snapshot's `plan`, which says which assignment a module's quiz or discussion is.
    `plan_items` is the class's approved document plan (week_plan.py); with one, the
    plan decides the to-dos, and a Canvas module item it does not mention is shown as a
    link unless it is a dated deadline. When `week_no` is the current week, unticked
    rows from earlier weeks come back as `carried`, so a reading skipped last week is
    not silently gone.
    """
    marks = marks or {}
    materials = materials or {}
    plan_items = plan_items or []
    items = [dict(i) for i in items]
    by_key = {i.get("import_key"): i for i in items if i.get("import_key")}
    by_title = {}
    for i in items:
        by_title.setdefault(canvas_norm(i.get("title")), i)
    via = {}
    for p in (plan or {}).get("items") or []:
        key = p.get("importKey")
        via[("Assignment", p.get("canvasId"))] = key
        if p.get("quizId"):
            via[("Quiz", p["quizId"])] = key
        if p.get("discussionId"):
            via[("Discussion", p["discussionId"])] = key

    rows, used_items = [], set()
    topics, notes = {}, {}

    def mark(key):
        return marks.get(key) or {}

    # The document's plan first, when there is one.
    plan_rows = []
    for p in plan_items:
        day = parse_iso(p.get("day"))
        if not day:
            continue
        n = week_number(day, term_start)
        kind = p.get("kind") or "work"
        if kind == "topic":
            topics.setdefault(n, p.get("title") or "")
            continue
        if kind == "note":
            notes.setdefault(n, []).append(p.get("title") or "")
            continue
        m = mark("plan:%s" % p["id"])
        dated = kind in ("assignment", "quiz", "exam", "discussion")
        row = {"key": "plan:%s" % p["id"], "source": "plan", "week": n,
               "title": p.get("title") or "", "kind": kind, "task": True,
               "done": bool(m.get("done")), "itemId": None, "url": None,
               "materialId": None, "section": "", "detail": p.get("detail") or "",
               "dueDate": p["day"] if dated else None, "dueTime": None,
               "alsoKeys": [], "_marked": "done" in m}
        best, score = None, 0.0
        for i in items:
            if i["id"] in used_items:
                continue
            due = parse_iso(i.get("due_date"))
            if due and abs((due - day).days) > 3:
                continue
            s = title_match(row["title"], i.get("title"))
            if s > score:
                best, score = i, s
        if best is not None and score >= MATCH_AT:
            used_items.add(best["id"])
            row["itemId"] = best["id"]
            row["done"] = best.get("status") == "done"
        plan_rows.append(row)

    for e in (course or {}).get("entries") or []:
        item = None
        if e["canvasType"] in ("Assignment", "Quiz", "Discussion"):
            item = by_key.get(via.get((e["canvasType"], e["contentId"])))
            # Canvas does not say which assignment a module's discussion is (the
            # discussion topic is not part of an assignment unless asked for), so
            # SD 381's discussions are found by their title.
            if item is None:
                item = (by_title.get(canvas_norm(e.get("rawTitle")))
                        or by_title.get(canvas_norm(e["title"])))
        m = mark(e["key"])
        row = dict(e, source="canvas", itemId=None, materialId=None)
        if m.get("as_task") is not None:
            row["task"] = bool(m["as_task"])
        if item is not None:
            row["itemId"] = item["id"]
            row["done"] = item.get("status") == "done"
            # His copy of the date wins: he may have moved it, and it is what the rest
            # of Vesta shows.
            if item.get("due_date"):
                row["dueDate"], row["dueTime"] = item["due_date"], item.get("due_time")
                row["week"] = week_number(parse_iso(item["due_date"]), term_start)
        else:
            row["done"] = bool(m["done"]) if "done" in m else e["canvasDone"]
        if e["canvasType"] == "File" and e.get("contentId") is not None:
            row["materialId"] = materials.get("canvas:file:%s" % e["contentId"])
        if plan_rows and row["task"]:
            # The course's schedule and its modules are merged. A Canvas item the
            # schedule also lists is the same to-do: it lends the schedule's row its
            # link and its assignment, and is not shown twice. One the schedule does
            # not list stays as it is. Since plans are read automatically for every
            # course (2026-09-24), turning unlisted module items into links would hide
            # real work in PSYC 300W, REM 388 and SD 381, whose modules are good.
            twin = max(plan_rows, key=lambda r: (abs(r["week"] - row["week"]) <= 1
                                                 and title_match(r["title"], row["title"])))
            if (abs(twin["week"] - row["week"]) <= 1
                    and title_match(twin["title"], row["title"]) >= MATCH_AT):
                twin["url"] = twin["url"] or row.get("url")
                twin["materialId"] = twin["materialId"] or row.get("materialId")
                # A tick given to the Canvas row before the schedule was read carries
                # over, and ticking the merged row ticks both from then on.
                twin["alsoKeys"].append(row["key"])
                if row["done"] and not twin["_marked"]:
                    twin["done"] = True
                if row["itemId"] and not twin["itemId"] and row["itemId"] not in used_items:
                    twin["itemId"] = row["itemId"]
                    twin["done"] = row["done"]
                    used_items.add(row["itemId"])
                continue

        if row["itemId"]:
            used_items.add(row["itemId"])
        rows.append(row)
    for r in plan_rows:
        r.pop("_marked", None)
    rows = plan_rows + rows

    weights = effective_weights(items, categories)
    plan_has_cues = any(r["kind"] == "start" for r in plan_rows)
    for i in items:
        due = parse_iso(i.get("due_date"))
        if due and i["id"] not in used_items:
            rows.append({
                "key": "item:%s" % i["id"], "source": "assignment",
                "week": week_number(due, term_start), "title": i.get("title") or "",
                "kind": (i.get("type") or "assignment").lower(), "task": True,
                "done": i.get("status") == "done", "itemId": i["id"],
                "dueDate": i.get("due_date"), "dueTime": i.get("due_time"),
                "url": None, "materialId": None, "section": "",
            })
        # A document that gives its own start dates is followed instead of Vesta's.
        cue = None if plan_has_cues else cue_for(i, weights.get(i["id"]))
        if cue:
            day = parse_iso(cue["date"])
            m = mark(cue["key"])
            rows.append(dict(cue, source="cue", task=True, done=bool(m.get("done")),
                             week=week_number(day, term_start), url=None,
                             materialId=None, section=""))

    this_week = [r for r in rows if r["week"] == week_no]
    carried = []
    if week_no == today_week:
        carried = [r for r in rows if 1 <= r["week"] < week_no and r["task"]
                   and not r["done"]]
    order = {"start": 0, "exam": 1, "quiz": 2, "assignment": 3, "discussion": 4,
             "reading": 5, "watch": 6, "work": 7}
    this_week.sort(key=lambda r: (not r["task"], order.get(r["kind"], 8),
                                  r.get("dueDate") or "9999", r["title"].lower()))
    carried.sort(key=lambda r: (r["week"], r.get("dueDate") or "9999"))
    info = ((course or {}).get("weeks") or {}).get(week_no) or {}
    tasks = [r for r in this_week if r["task"]]
    return {
        "classId": cls["id"], "code": cls.get("code") or "", "name": cls.get("name") or "",
        "color": cls.get("color") or "",
        "topic": topics.get(week_no) or info.get("topic") or "",
        "notes": notes.get(week_no) or info.get("notes") or [],
        "connected": course is not None,
        "planned": bool(plan_items),
        "planSource": (plan_items[0].get("source") or "") if plan_items else "",
        "offset": (course or {}).get("offset"), "offsetSource": (course or {}).get("offsetSource"),
        "tasks": tasks,
        "links": [r for r in this_week if not r["task"]],
        "carried": carried,
        "done": sum(1 for r in tasks if r["done"]), "total": len(tasks),
    }


def build_week(conn, start, today=None):
    """The whole week starting on `start` (any day in it will do), for the active term."""
    import db
    import canvas_sync
    import ics

    sem = _row(db.active_semester(conn))
    term_start = parse_iso(sem.get("start_date")) or monday_of(start)
    week_no = week_number(start, term_start)
    today_week = week_number(today, term_start) if today else None
    classes = [_row(r) for r in conn.execute(
        "SELECT id, code, name, color FROM classes WHERE semester_id=? ORDER BY code, name",
        (sem["id"],)).fetchall()]
    ids = [c["id"] for c in classes]
    items_by, cats_by, materials = {}, {}, {}
    if ids:
        marks_q = ",".join("?" * len(ids))
        for r in conn.execute(
                "SELECT id, class_id, title, type, due_date, due_time, status, weight,"
                " category_id, import_key FROM items WHERE class_id IN (%s)" % marks_q,
                ids).fetchall():
            items_by.setdefault(r["class_id"], []).append(_row(r))
        for r in conn.execute(
                "SELECT id, class_id, weight FROM grade_categories WHERE class_id IN (%s)"
                % marks_q, ids).fetchall():
            cats_by.setdefault(r["class_id"], []).append(_row(r))
        for r in conn.execute(
                "SELECT id, import_key FROM materials WHERE class_id IN (%s)"
                " AND import_key IS NOT NULL" % marks_q, ids).fetchall():
            materials[r["import_key"]] = r["id"]
    marks = {r["key"]: _row(r) for r in conn.execute(
        "SELECT key, done, as_task FROM week_marks").fetchall()}
    import week_plan
    plans = week_plan.plan_rows(conn, ids)

    state = canvas_sync.load_state(conn)
    course_of = {e.get("classId"): cid for cid, e in (state.get("courses") or {}).items()
                 if e.get("classId")}
    out = []
    for cls in classes:
        course = plan = None
        cid = course_of.get(cls["id"])
        snap = canvas_sync.load_snapshot(conn, cid) if cid else None
        if snap and snap.get("modules") is not None:
            tz = ics.zone(snap.get("timeZone")) if snap.get("timeZone") else None
            shift = ((state.get("courses") or {}).get(cid) or {}).get("weekShift")
            course = course_entries(cid, snap["modules"], term_start, tz, shift=shift)
            plan = snap.get("plan")
        elif snap:
            plan = snap.get("plan")
        one = assemble_class(
            cls, term_start, week_no, today_week, course=course, plan=plan,
            items=items_by.get(cls["id"], []), categories=cats_by.get(cls["id"], []),
            materials=materials, marks=marks, plan_items=plans.get(cls["id"]))
        # Mapped to Canvas, but the last check predates modules being read (or has not
        # happened yet): one check fills it in, and the page offers it.
        one["needsCheck"] = bool(cid) and course is None
        one["canvasMapped"] = bool(cid)
        one["plan"] = week_plan.public_state(week_plan.load_state(conn, cls["id"]))
        out.append(one)
    # Classes off Canvas are planned from their own files when this page is opened,
    # since there is no Canvas check to do it after.
    waiting = [c["classId"] for c in out if not c["canvasMapped"]
               and week_plan.needs_plan(conn, c["classId"])]
    planning = week_plan.plan_soon(db.current_user_id(), waiting) if waiting else []
    ws = week_start(week_no, term_start)
    return {
        "planning": planning,
        "weekStart": ws.isoformat(), "weekEnd": (ws + timedelta(days=6)).isoformat(),
        "week": week_no, "termStart": term_start.isoformat(),
        "termEnd": sem.get("end_date") or "",
        "classes": out,
        "canvasChecked": state.get("lastCheck"),
    }


def set_mark(conn, key, class_id=None, done=None, as_task=None, clear_as_task=False):
    """Remember a tick, or a row switched between checkbox and link.

    Update-then-insert, for the same reason as `db.set_setting`: the key is unique per
    account on Postgres and unique outright on SQLite, so no ON CONFLICT target is
    right in both.
    """
    from datetime import datetime
    now = datetime.utcnow().isoformat()
    row = conn.execute("SELECT key, done, as_task FROM week_marks WHERE key=?", (key,)).fetchone()
    new_done = (1 if done else 0) if done is not None else (row["done"] if row else 0)
    if clear_as_task:
        new_task = None
    elif as_task is not None:
        new_task = 1 if as_task else 0
    else:
        new_task = row["as_task"] if row else None
    if row:
        conn.execute("UPDATE week_marks SET done=?, as_task=?, updated_at=? WHERE key=?",
                     (new_done, new_task, now, key))
    else:
        conn.execute("INSERT INTO week_marks (key, class_id, done, as_task, updated_at)"
                     " VALUES (?,?,?,?,?)", (key, class_id, new_done, new_task, now))


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

from flask import Blueprint, jsonify, request  # noqa: E402

bp = Blueprint("week", __name__)

KEY_RE = re.compile(r"^(canvas:[\w-]+:mi:\d+|cue:[\w-]+|plan:[\w-]+)$")


def _day(value, fallback=None):
    return parse_iso(value) or fallback


@bp.route("/api/week", methods=["GET"])
def get_week():
    import db
    today = _day(request.args.get("today"), date.today())
    start = _day(request.args.get("start"), today)
    conn = db.get_db()
    try:
        return jsonify(build_week(conn, start, today))
    finally:
        conn.close()


@bp.route("/api/week/marks", methods=["POST"])
def post_marks():
    """Tick, untick, or switch rows. Takes {"marks": [{key, classId, done?, asTask?}]}.

    Only module rows and cues are marked here. An assignment's tick is the assignment's
    own status, set through `/api/items/<id>` like everywhere else in Vesta, so the two
    can never disagree.
    """
    import db
    data = request.get_json(silent=True) or {}
    marks = data.get("marks") or []
    if not isinstance(marks, list) or not marks or len(marks) > 500:
        return jsonify({"error": "Nothing to mark."}), 400
    conn = db.get_db()
    try:
        for m in marks:
            key = str((m or {}).get("key") or "")
            if not KEY_RE.match(key):
                return jsonify({"error": "That row cannot be marked."}), 400
            set_mark(conn, key, class_id=m.get("classId") or None,
                     done=m.get("done") if "done" in m else None,
                     as_task=m.get("asTask") if m.get("asTask") is not None else None,
                     clear_as_task="asTask" in m and m.get("asTask") is None)
        conn.commit()
        return jsonify({"ok": True, "marked": len(marks)})
    finally:
        conn.close()


@bp.route("/api/week/shift", methods=["POST"])
def post_shift():
    """Set, or clear, a Canvas course's week shift by hand. Takes {classId, shift}.

    For when the offset worked out from the course's own dates is wrong. Stored on the
    course's entry in the Canvas state, beside the class it is mapped to.
    """
    import db
    import canvas_sync
    data = request.get_json(silent=True) or {}
    class_id = data.get("classId")
    shift = data.get("shift")
    if shift is not None:
        try:
            shift = int(shift)
        except (TypeError, ValueError):
            return jsonify({"error": "The shift must be a whole number of weeks."}), 400
        if abs(shift) > 4:
            return jsonify({"error": "A shift of more than four weeks is not a numbering difference."}), 400
    conn = db.get_db()
    try:
        state = canvas_sync.load_state(conn)
        for cid, entry in (state.get("courses") or {}).items():
            if entry.get("classId") == class_id:
                if shift is None:
                    entry.pop("weekShift", None)
                else:
                    entry["weekShift"] = shift
                canvas_sync.save_state(conn, state)
                return jsonify({"ok": True})
        return jsonify({"error": "That class is not connected to a Canvas course."}), 404
    finally:
        conn.close()

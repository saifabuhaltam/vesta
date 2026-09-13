"""Syllabus import: read a course syllabus, show what was found, import only what the
student confirms.

The flow has three stages, and nothing reaches the student's classes until the last.

1. **Read.** The uploaded document goes to Claude Opus 5 as the document itself (PDF
   pages keep their tables and layout), constrained to a fixed JSON shape. Cost is
   estimated first and the usual daily cap and confirmation rules apply.
2. **Review.** The raw reading is turned into a draft: week numbers become dates,
   repeating work becomes one item per date, anything inferred or ambiguous is marked
   as needing a look, and SFU's official outline is used to fill gaps and flag
   disagreements. For a class that already exists, each item is compared with what is
   there. The draft is stored and shown to the student to correct.
3. **Import.** Only the reviewed payload is written, in one transaction.
"""
import base64
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta

import anthropic

import ai
from db import get_db

IMPORT_MODEL = "claude-opus-5"
IMPORT_MAX_TOKENS = 32000
EXPECTED_OUTPUT_TOKENS = 9000        # thinking plus the JSON for a busy course
TOKENS_PER_PDF_PAGE = 2000           # text and page image together, a working average
TOKENS_PER_IMAGE = 1600
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ITEM_TYPES = ["assignment", "homework", "quiz", "exam", "reading", "discussion", "project", "other"]
MEETING_KINDS = ["lecture", "lab", "tutorial", "seminar", "other"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------------------
# What Claude must return
# ---------------------------------------------------------------------------
def _null(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(props, required=None):
    return {"type": "object", "properties": props,
            "required": required if required is not None else list(props.keys()),
            "additionalProperties": False}


STR, INT, NUM, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
# Two limits on structured outputs shape this schema, both found against the live API:
#   - at most 16 nullable or union-typed fields, so nothing here is nullable: text that
#     is not given is "", numbers not given are 0;
#   - a cap on the compiled grammar, which enums and nested objects inflate, so allowed
#     values live in descriptions and a repeat's details sit flat on the assessment.
# normalize_raw turns placeholders back into None and cleans up the free-text values.
DATE = {"type": "string", "description": "YYYY-MM-DD, or empty string if not stated"}
HHMM = {"type": "string", "description": "24-hour HH:MM, or empty string if not stated"}
TEXT = {"type": "string", "description": "empty string if not stated"}
DAY = {"type": "string", "description": "Mon, Tue, Wed, Thu, Fri, Sat or Sun; empty string if not stated"}
ZERO = lambda what: {"type": "integer", "description": what + "; 0 if not stated"}

SCHEMA = _obj({
    "course": _obj({
        "code": TEXT, "title": TEXT, "section": TEXT,
        "term": {"type": "string", "description": "e.g. Fall 2026, or empty string"},
        "instructor": TEXT, "instructor_email": TEXT, "website": TEXT,
        "first_day": DATE, "last_day": DATE,
    }),
    "meetings": {"type": "array", "items": _obj({
        "kind": {"type": "string", "description": "lecture, lab, tutorial, seminar or other"},
        "days": {"type": "array", "items": DAY},
        "start": HHMM, "end": HHMM, "location": TEXT, "section": TEXT, "evidence": STR,
    })},
    "grade_scale": {"type": "array", "items": _obj({"letter": STR, "min_percent": NUM})},
    "grade_categories": {"type": "array", "items": _obj({
        "name": STR,
        "weight_percent": {"type": "number", "description": "0 if not stated"},
        "item_count": ZERO("how many items share it"),
        "drop_lowest": INT, "evidence": STR,
    })},
    "assessments": {"type": "array", "items": _obj({
        "title": STR,
        "type": {"type": "string", "description": "assignment, homework, quiz, exam, reading, discussion, project or other"},
        "category": {"type": "string", "description": "name of a grade_categories entry, or empty string"},
        "weight_percent": {"type": "number", "description": "this item's own weight; 0 when it shares a category weight or none is given"},
        "due_date": DATE, "due_time": HHMM,
        "date_as_written": TEXT,
        "week": ZERO("week number when the date is given only as a week"),
        "weekday": DAY,
        "repeat_every_weeks": {"type": "integer", "description": "1 for weekly, 2 for every two weeks; 0 when it does not repeat"},
        "repeat_weekday": DAY,
        "repeat_first_date": DATE, "repeat_last_date": DATE,
        "repeat_first_week": ZERO("first week it repeats in"),
        "repeat_last_week": ZERO("last week it repeats in"),
        "repeat_skip_dates": {"type": "array", "items": {"type": "string", "description": "YYYY-MM-DD"}},
        "repeat_skip_weeks": {"type": "array", "items": INT},
        "location": TEXT,
        "sure": BOOL,
        "unsure_why": TEXT,
        "evidence": STR,
    })},
    "topics": {"type": "array", "items": _obj({"week": ZERO("week number"), "date": DATE, "title": STR})},
    "no_class_dates": {"type": "array", "items": _obj({"date": {"type": "string", "description": "YYYY-MM-DD"},
                                                       "label": STR})},
    "warnings": {"type": "array", "items": STR},
})

SYSTEM = """You read university course syllabi and extract the course's facts for a student's planner.
The student will review everything you return before it is saved, and a wrong date costs them a
missed deadline, so accuracy beats completeness.

Rules:
- Only report what the document states. Never invent a date, weight, room or name. When something
  is not given, use an empty string for text and 0 for numbers.
- `evidence` is a short verbatim quote from the document that supports the entry.
- Set `sure` to false, and say why in `unsure_why`, whenever a value was inferred rather than read
  directly: a date computed from a week number, "TBA"/"TBD" or "during the exam period", two dates
  that disagree, a weight that does not add up, a table you could not read cleanly, a typo in a
  year or date, or a due date given only as "in class", "end of week" or "Week 6".
- If the document never states a year, use the year of the student's current term (given below) or of
  the file name. Never choose a year from weekday arithmetic alone: the same weekdays repeat across years.
- The file name often carries the course code and term. Use it when the document itself does not state
  them, and say so in `warnings`.
- "Due end of Week N" means the last day of week N as the document's own schedule defines it: set
  `week` to N and `weekday` to that last day (for weeks listed Monday to Sunday, Sun).
- Give `due_date` only when a calendar date is stated. If only a week is given, set `week` (use the
  document's own week numbering) and `weekday` if stated, leave `due_date` empty, and mark it unsure.
- List every individually named or numbered assessment (Quiz 1, Quiz 2...). For work that recurs on a
  pattern without being listed one by one ("reading response due every Friday"), return one entry
  with the `repeat_` fields filled in (`repeat_every_weeks` 1 for weekly) instead of guessing each date.
  For anything that does not repeat, set `repeat_every_weeks` to 0.
- When several items share a category weight ("Quizzes 20%, best 8 of 10"), create one
  `grade_categories` entry (weight 20, item_count 10, drop_lowest 2), set each assessment's `category`
  to its name, and set that assessment's `weight_percent` to 0. Items with their own weight get
  `weight_percent` and an empty `category`.
- Each graded thing gets its weight exactly once: EITHER its own `weight_percent` with an empty
  `category`, OR a `category` with `weight_percent` 0. Never create a grade category for items that
  already carry their own weights ("2 midterms, 15% each" is two items at 15, not a category too).
  A category with a single item is not a category.
- Participation or attendance with a weight but no date is one undated assessment: `due_date` empty,
  `repeat_every_weeks` 0, and `sure` true if the weight is clear. Put "miss one without penalty" in
  `evidence`, not in dates.
- `meetings` are scheduled class sessions only: lectures, labs, tutorials, seminars. Office hours,
  TA hours and drop-in sessions are never meetings.
- `grade_scale` only when the syllabus gives letter-grade cutoffs. Use the lowest percentage for each
  letter.
- `no_class_dates` lists days classes or sections are cancelled. Work can still be due on them; do not
  drop an assessment just because it falls on one.
- Times are 24-hour HH:MM. Days are Mon..Sun.
- Put anything the student should double-check that does not fit elsewhere in `warnings`."""


# ---------------------------------------------------------------------------
# Sending the document
# ---------------------------------------------------------------------------
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def ext_of(filename):
    return (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def pdf_pages(path):
    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except Exception:
        return 12


def document_block(path, filename, extracted_text):
    """The syllabus as Claude should see it: the real PDF or image where possible, text otherwise."""
    ext = ext_of(filename)
    if ext == "pdf":
        with open(path, "rb") as fh:
            data = base64.standard_b64encode(fh.read()).decode("ascii")
        return ({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
                pdf_pages(path) * TOKENS_PER_PDF_PAGE)
    if ext in IMAGE_TYPES:
        with open(path, "rb") as fh:
            data = base64.standard_b64encode(fh.read()).decode("ascii")
        return ({"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[ext], "data": data}},
                TOKENS_PER_IMAGE)
    text = (extracted_text or "").strip()
    if not text:
        raise ai.AiRefused({"error": "Vesta could not read any text from that file. "
                                     "Try a PDF, a Word document, or a photo of the syllabus."}, 400)
    return ({"type": "text", "text": "The syllabus:\n\n" + text}, ai.estimate_tokens(text))


def estimate(conn, input_tokens):
    cfg = ai.settings(conn)
    usd = ai.estimate_cost(cfg, input_tokens + 1500, EXPECTED_OUTPUT_TOKENS, model=IMPORT_MODEL)
    return {"estimateUsd": round(usd, 3), "inputTokens": input_tokens, "model": IMPORT_MODEL,
            "spentToday": ai.spent_today(conn, cfg)["usd"], "dailyCap": cfg["daily_cap_usd"]}


def read_syllabus(conn, block, input_tokens, context_note, confirmed):
    """One paid call. Refuses before spending if the day's cap is reached or it is unconfirmed."""
    cfg = ai.settings(conn)
    est = estimate(conn, input_tokens)
    if cfg["daily_cap_usd"] and est["spentToday"] + est["estimateUsd"] > cfg["daily_cap_usd"]:
        raise ai.AiRefused(dict(est, error="Reading this syllabus would go past today's AI limit.",
                                reason="daily_cap"), 402)
    if not confirmed:
        raise ai.AiRefused(dict(est, error="Confirm before Vesta reads the syllabus.", reason="confirm"), 409)

    request = dict(
        model=IMPORT_MODEL, max_tokens=IMPORT_MAX_TOKENS, system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            block,
            {"type": "text", "text": "Extract this course's details." + (("\n\n" + context_note) if context_note else "")},
        ]}],
    )
    client = anthropic.Anthropic()
    try:
        try:
            # a declined request is re-run on Anthropic's recommended fallback model
            with client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **request) as stream:
                msg = stream.get_final_message()
        except anthropic.BadRequestError as e:
            if "fallback" not in str(e).lower():
                raise
            with client.messages.stream(**request) as stream:
                msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise ai.AiRefused({"error": "The Anthropic API key is missing or was rejected. "
                                     "Add ANTHROPIC_API_KEY to the .env file next to app.py."}, 503)
    except anthropic.RateLimitError:
        raise ai.AiRefused({"error": "Anthropic is rate limiting right now. Wait a minute and try again."}, 429)
    except anthropic.APIConnectionError:
        raise ai.AiRefused({"error": "Could not reach the Anthropic API."}, 502)
    except anthropic.APIStatusError as e:
        raise ai.AiRefused({"error": f"Anthropic error: {e.message}"}, 502)

    usage = msg.usage
    ai.record_usage(conn, "syllabus", msg.model, usage.input_tokens, usage.output_tokens)
    if msg.stop_reason == "refusal":
        raise ai.AiRefused({"error": "The model declined to read this document."}, 422)
    if msg.stop_reason == "max_tokens":
        raise ai.AiRefused({"error": "The syllabus was too long to read in one go."}, 422)
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        raise ai.AiRefused({"error": "The reading came back malformed. Try again."}, 502)
    return data, {"model": msg.model, "inputTokens": usage.input_tokens, "outputTokens": usage.output_tokens}


# ---------------------------------------------------------------------------
# SFU's official outline
#
# Public and free. It knows the instructor, the real meeting days and dates, and the
# final exam slot, which syllabi often leave as "TBA". It usually lacks rooms and
# grade weights, so it fills gaps and flags disagreements rather than overriding.
# ---------------------------------------------------------------------------
SFU_BASE = "https://www.sfu.ca/bin/wcm/course-outlines"
SFU_DAYS = {"Su": 0, "Mo": 1, "Tu": 2, "We": 3, "Th": 4, "Fr": 5, "Sa": 6}
SFU_KINDS = {"LEC": "lecture", "LAB": "lab", "TUT": "tutorial", "SEM": "seminar"}
DAY_INDEX = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}


def split_code(code):
    m = re.match(r"^\s*([A-Za-z]{2,5})\s*[-_]?\s*(\d{3}[A-Za-z]?)(?![0-9])", code or "")
    return (m.group(1).lower(), m.group(2).lower()) if m else (None, None)


def split_term(term):
    m = re.search(r"(spring|summer|fall|autumn)\D*(\d{4})", (term or "").lower())
    if not m:
        return None
    return m.group(2), ("fall" if m.group(1) == "autumn" else m.group(1))


def fetch_json(url, timeout=8):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Vesta student planner"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sfu_outline_path(code, section, term, fetch=fetch_json):
    """Work out outlines.sfu.ca's path, asking SFU for the section list if none was given."""
    dept, number = split_code(code)
    t = split_term(term)
    if not dept or not t:
        return None
    base = f"{t[0]}/{t[1]}/{dept}/{number}"
    if section:
        return f"{base}/{section.strip().lower()}"
    try:
        sections = fetch(f"{SFU_BASE}?{base}")
    except Exception:
        return None
    enrol = [s.get("value") for s in sections if isinstance(s, dict) and s.get("classType") == "e"]
    return f"{base}/{enrol[0]}" if len(enrol) == 1 else None


def sfu_date(text):
    """'Wed Sep 03 00:00:00 PDT 2025' -> date."""
    m = re.match(r"^\w{3} (\w{3}) (\d{2}) [\d:]+ \w+ (\d{4})$", (text or "").strip())
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
    except ValueError:
        return None


def parse_sfu_outline(data):
    info = data.get("info") or {}
    pi = next((p for p in (data.get("instructor") or []) if p.get("roleCode") == "PI"),
              (data.get("instructor") or [None])[0])
    def where(s):
        room = s.get("roomNumber") and f"{s.get('buildingCode', '')} {s.get('roomNumber')}".strip()
        return ", ".join(x for x in [s.get("campus"), room] if x)

    course_rows = data.get("courseSchedule") or []
    exam_rows = data.get("examSchedule") or []

    meetings = []
    for s in course_rows:
        if s.get("isExam"):
            continue
        days = [SFU_DAYS[d.strip()] for d in (s.get("days") or "").split(",") if d.strip() in SFU_DAYS]
        meetings.append({
            "kind": SFU_KINDS.get((s.get("sectionCode") or "").upper(), "lecture"),
            "days": days, "start": s.get("startTime"), "end": s.get("endTime"),
            "location": where(s),
            "startDate": sfu_date(s.get("startDate")), "endDate": sfu_date(s.get("endDate")),
        })

    # Finals live in examSchedule. Checked against 16 real sections across fall 2025 and
    # spring 2026: not one carried an isExam row in courseSchedule, so the second list
    # here is a guard that has never fired, not a fix for an observed bug. A section with
    # no examSchedule simply has no exam published yet, which is normal early in a term,
    # and some courses (IAT 201) never have one.
    exams = [e for e in exam_rows if e.get("isExam")] or [s for s in course_rows if s.get("isExam")]
    final = None
    if exams:
        e = exams[0]
        final = {"date": sfu_date(e.get("startDate")), "start": e.get("startTime"),
                 "end": e.get("endTime"), "location": where(e)}
    starts = [m["startDate"] for m in meetings if m["startDate"]]
    ends = [m["endDate"] for m in meetings if m["endDate"]]
    return {
        "code": f"{info.get('dept', '')} {info.get('number', '')}".strip(),
        "title": info.get("title") or "", "section": info.get("section") or "",
        "term": info.get("term") or "",
        "instructor": (pi or {}).get("name") or "", "instructorEmail": (pi or {}).get("email") or "",
        "meetings": meetings, "finalExam": final,
        "firstDay": min(starts) if starts else None, "lastDay": max(ends) if ends else None,
        "path": info.get("outlinePath") or "",
    }


def official_outline(code, section, term, fetch=fetch_json):
    path = sfu_outline_path(code, section, term, fetch)
    if not path:
        return None, "SFU's official outline could not be matched to this course and term."
    try:
        return parse_sfu_outline(fetch(f"{SFU_BASE}?{path}")), None
    except Exception:
        return None, "SFU has not published an official outline for this section yet."


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def to_date(value):
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else None
    except (TypeError, ValueError):
        return None


def monday_of(d):
    return d - timedelta(days=d.weekday())


def resolve_week(first_day, week, weekday=None, meeting_days=(), avoid=()):
    """Week 1 is the Monday-to-Sunday week containing the first day of classes.

    With no weekday, the first class meeting in that week is used, since "Week 6
    quiz" nearly always means in class. Returns None when there is nothing to anchor to.
    """
    if not first_day or not week or week < 1:
        return None
    start = monday_of(first_day) + timedelta(weeks=week - 1)
    if weekday in DAY_INDEX:
        return start + timedelta(days=(DAY_INDEX[weekday] - 1) % 7)
    if not meeting_days:
        return None
    # the first class that week that is actually held (not a holiday)
    options = [start + timedelta(days=(d - 1) % 7) for d in sorted(meeting_days, key=lambda d: (d - 1) % 7)]
    return next((d for d in options if d not in avoid), options[0])


def expand_repeats(rep, first_day, last_day, no_class=()):
    """Every date a repeating piece of work falls on, skipping breaks and stated gaps."""
    step = max(1, rep.get("every_weeks") or 1) * 7
    idx = DAY_INDEX.get(rep.get("weekday"))
    if idx is None:
        return []
    start = to_date(rep.get("first_date"))
    if not start and rep.get("first_week"):
        start = resolve_week(first_day, rep["first_week"], rep["weekday"])
    if not start and first_day:
        start = first_day + timedelta(days=(idx - first_day.isoweekday() % 7) % 7)
    end = to_date(rep.get("last_date"))
    if not end and rep.get("last_week"):
        end = resolve_week(first_day, rep["last_week"], rep["weekday"])
    end = end or last_day
    if not start or not end or end < start:
        return []
    # only the syllabus's own stated gaps are skipped; a cancelled class does not cancel
    # work that is submitted online, so those dates are kept and flagged instead
    skip = {to_date(d) for d in rep.get("skip_dates") or []}
    skip_weeks = set(rep.get("skip_weeks") or [])
    out, d = [], start
    while d <= end and len(out) < 40:
        week = ((monday_of(d) - monday_of(first_day)).days // 7 + 1) if first_day else None
        if d not in skip and week not in skip_weeks:
            out.append(d)
        d += timedelta(days=step)
    return out


# ---------------------------------------------------------------------------
# The review draft
# ---------------------------------------------------------------------------
def new_id():
    return uuid.uuid4().hex[:12]


def clean_time(t):
    m = re.match(r"^(\d{1,2}):(\d{2})", t or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def same_person(a, b):
    last = lambda s: (re.findall(r"[a-z]+", (s or "").lower()) or [""])[-1]
    return bool(a and b and last(a) == last(b))


DAY_ALIASES = {"mon": "Mon", "monday": "Mon", "tue": "Tue", "tues": "Tue", "tuesday": "Tue",
               "wed": "Wed", "weds": "Wed", "wednesday": "Wed", "thu": "Thu", "thur": "Thu", "thurs": "Thu",
               "thursday": "Thu", "fri": "Fri", "friday": "Fri", "sat": "Sat", "saturday": "Sat",
               "sun": "Sun", "sunday": "Sun"}
TYPE_ALIASES = {"midterm": "exam", "midterm exam": "exam", "final": "exam", "final exam": "exam", "test": "exam",
                "examination": "exam", "essay": "assignment", "paper": "assignment", "report": "assignment",
                "lab": "assignment", "presentation": "project", "participation": "other", "attendance": "other",
                "discussion post": "discussion", "reading response": "reading"}
KIND_ALIASES = {"lec": "lecture", "tut": "tutorial", "sem": "seminar", "laboratory": "lab", "section": "tutorial"}


def norm_day(v):
    return DAY_ALIASES.get((v or "").strip().lower().rstrip("."))


def norm_type(v):
    v = (v or "").strip().lower()
    return v if v in ITEM_TYPES else TYPE_ALIASES.get(v, "other")


def norm_kind(v):
    v = (v or "").strip().lower()
    return v if v in MEETING_KINDS else KIND_ALIASES.get(v, "other")


def normalize_raw(raw):
    """Turn the schema's "" and 0 placeholders back into None and tidy free-text values.

    Accepts the flat `repeat_*` fields the model returns, or a nested `repeats` object as
    older drafts and tests use. Safe to run twice.
    """
    raw = json.loads(json.dumps(raw or {}))
    for m in raw.get("meetings") or []:
        m["kind"] = norm_kind(m.get("kind"))
        m["days"] = [d for d in (norm_day(x) for x in m.get("days") or []) if d]
    for a in raw.get("assessments") or []:
        a["type"] = norm_type(a.get("type"))
        if "repeat_every_weeks" in a:
            a["repeats"] = {
                "every_weeks": a.pop("repeat_every_weeks"), "weekday": a.pop("repeat_weekday", ""),
                "first_date": a.pop("repeat_first_date", ""), "last_date": a.pop("repeat_last_date", ""),
                "first_week": a.pop("repeat_first_week", 0), "last_week": a.pop("repeat_last_week", 0),
                "skip_dates": a.pop("repeat_skip_dates", []), "skip_weeks": a.pop("repeat_skip_weeks", []),
            }
    blank = lambda v: None if v in ("", None) else v
    zero = lambda v: None if not v else v
    c = raw.get("course") or {}
    raw["course"] = {k: blank(v) for k, v in c.items()}
    for m in raw.get("meetings") or []:
        for k in ("start", "end", "location", "section"):
            m[k] = blank(m.get(k))
    for g in raw.get("grade_categories") or []:
        g["weight_percent"] = zero(g.get("weight_percent"))
        g["item_count"] = zero(g.get("item_count"))
        g["drop_lowest"] = g.get("drop_lowest") or 0
    for a in raw.get("assessments") or []:
        for k in ("category", "due_date", "due_time", "date_as_written", "location", "unsure_why"):
            a[k] = blank(a.get(k))
        a["weekday"] = norm_day(a.get("weekday"))
        a["weight_percent"] = zero(a.get("weight_percent"))
        a["week"] = zero(a.get("week"))
        r = a.get("repeats")
        if not r or not r.get("every_weeks"):
            a["repeats"] = None
        else:
            for k in ("first_date", "last_date"):
                r[k] = blank(r.get(k))
            r["weekday"] = norm_day(r.get("weekday"))
            r["first_week"], r["last_week"] = zero(r.get("first_week")), zero(r.get("last_week"))
            r["skip_dates"] = [d for d in r.get("skip_dates") or [] if d]
    for t in raw.get("topics") or []:
        t["week"], t["date"] = zero(t.get("week")), blank(t.get("date"))
    raw["no_class_dates"] = [x for x in raw.get("no_class_dates") or [] if x.get("date")]
    return raw


ONGOING = re.compile(r"\b(attend|particip|engagement|contribution)", re.I)
OFFICE = re.compile(r"\boffice\s*hours?\b|\bdrop-?in\b|\bTA hours?\b", re.I)


STOPWORDS = {"and", "the", "of", "in", "a", "an", "for", "on", "to", "each", "total"}


def _tokens(text):
    """Lower-case words with plurals folded ("quizzes" -> "quiz", "exams" -> "exam")."""
    out = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if w in STOPWORDS:
            continue
        for suffix in ("zes", "es", "s"):
            if w.endswith(suffix) and len(w) > len(suffix) + 2 and not w.endswith("ss"):
                w = w[: -len(suffix)]
                break
        out.add(w)
    return out


def reconcile(raw):
    """Catch the reading's own inconsistencies before they reach the student.

    - Office hours are not class meetings.
    - Attendance and participation are term-long grades, not repeating deadlines.
    - A grade category whose weight is already carried by individually weighted items
      (a "Midterms 30%" category next to two midterms at 15% each) is a duplicate: left
      in, it double-counts the weight. So is a category of one.
    """
    raw["meetings"] = [m for m in raw.get("meetings") or []
                       if not OFFICE.search((m.get("evidence") or "") + " " + (m.get("section") or ""))]
    for a in raw.get("assessments") or []:
        if a.get("repeats") and ONGOING.search(a.get("title") or "") and not a["repeats"].get("weekday"):
            a["repeats"] = None
            a["sure"] = True if a.get("weight_percent") or a.get("category") else a.get("sure")
            a["unsure_why"] = None if a["sure"] else a.get("unsure_why")
    items = raw.get("assessments") or []
    keep = []
    for g in raw.get("grade_categories") or []:
        name = (g.get("name") or "").strip().lower()
        members = [a for a in items if (a.get("category") or "").strip().lower() == name]
        loose = [a for a in items if not a.get("category") and a.get("weight_percent")]
        weight = g.get("weight_percent") or 0
        # Match on title words, one word at a time: "Midterm exams" is the midterms (by
        # "midterm"), not every exam, which would sweep in the final as well.
        duplicate = False
        if not members and weight:
            for word in _tokens(g.get("name")):
                group = [a for a in loose if word in _tokens(a.get("title"))]
                if group and abs(sum(a["weight_percent"] for a in group) - weight) < 0.51:
                    duplicate = True
                    break
        if duplicate:
            continue
        if len(members) == 1 and not g.get("drop_lowest"):
            # one item: it simply carries the category's weight itself
            members[0]["category"] = None
            members[0]["weight_percent"] = members[0].get("weight_percent") or weight or None
            continue
        keep.append(g)
    raw["grade_categories"] = keep
    return raw


def hints_from_filename(name):
    """'SD_381_Fall_2026_Online.pdf' -> code SD 381, term Fall 2026."""
    dept, number = split_code(name or "")
    t = split_term((name or "").replace("_", " "))
    return {"code": f"{dept.upper()} {number.upper()}" if dept else None,
            "term": f"{t[1].capitalize()} {t[0]}" if t else None}


def expected_year(course, hint, term):
    for text in (course.get("term"), (hint or {}).get("term"), (term or {}).get("name")):
        t = split_term(text)
        if t:
            return int(t[0])
    start = to_date((term or {}).get("startDate"))
    return start.year if start else None


def correct_years(raw, year):
    """Move dates the reading placed in the wrong year, when the weekday still matches.

    A syllabus with no year lets the model do weekday arithmetic, and Sept 9 is a
    Wednesday in 2020 as well as 2026. Returns how many dates moved; each moved
    assessment is marked so the review can say so.
    """
    moved = 0

    def fix(value):
        nonlocal moved
        d = to_date(value)
        if not d or not year or d.year == year:
            return value
        try:
            candidate = d.replace(year=year)
        except ValueError:
            return value
        if candidate.weekday() != d.weekday():
            return value
        moved += 1
        return candidate.isoformat()

    c = raw.get("course") or {}
    for k in ("first_day", "last_day"):
        c[k] = fix(c.get(k))
    for a in raw.get("assessments") or []:
        before = a.get("due_date")
        a["due_date"] = fix(before)
        if before and a["due_date"] != before:
            a["_year_fixed"] = int(before[:4])
        r = a.get("repeats")
        if r:
            r["first_date"], r["last_date"] = fix(r.get("first_date")), fix(r.get("last_date"))
            r["skip_dates"] = [fix(x) for x in r.get("skip_dates") or []]
    for t in raw.get("topics") or []:
        t["date"] = fix(t.get("date"))
    for x in raw.get("no_class_dates") or []:
        x["date"] = fix(x.get("date"))
    return moved


def build_draft(raw, official=None, official_note=None, term=None, hint=None):
    """Turn Claude's reading (plus SFU's outline) into something the student can review.

    `hint` is the uploaded file's name, which often carries the course code and term.
    """
    raw = reconcile(normalize_raw(raw))
    hint = hints_from_filename(hint) if isinstance(hint, str) else (hint or {})
    rc = raw.get("course") or {}
    from_filename = [k for k in ("code", "term") if not rc.get(k) and hint.get(k)]
    for k in from_filename:
        rc[k] = hint[k]
    raw["course"] = rc
    year = expected_year(rc, hint, term)
    years_moved = correct_years(raw, year)
    c = raw.get("course") or {}
    term = term or {}
    conflicts = []

    def pick(field, mine, theirs):
        if mine and theirs and str(mine).strip().lower() != str(theirs).strip().lower():
            if not (field == "professor" and same_person(mine, theirs)):
                conflicts.append({"field": field, "syllabus": mine, "official": theirs})
        return mine or theirs or ""

    off = official or {}
    course = {
        "code": pick("code", (c.get("code") or "").upper(), off.get("code", "").upper()),
        "name": c.get("title") or off.get("title") or "",
        "section": (c.get("section") or off.get("section") or "").upper(),
        "term": c.get("term") or off.get("term") or term.get("name") or "",
        "professor": pick("professor", c.get("instructor"), off.get("instructor")),
        "professorEmail": c.get("instructor_email") or off.get("instructorEmail") or "",
        "website": c.get("website") or "",
    }
    first_day = to_date(c.get("first_day")) or off.get("firstDay") or to_date(term.get("startDate"))
    last_day = to_date(c.get("last_day")) or off.get("lastDay") or to_date(term.get("endDate"))
    no_class = {to_date(x.get("date")) for x in raw.get("no_class_dates") or [] if to_date(x.get("date"))}
    # Some schedules list weeks Sunday to Saturday ("Week 01 Sep 06-12"). Counting weeks
    # Monday to Sunday from a Sunday start would put every week-numbered date a week early.
    week_anchor = first_day
    if first_day and first_day.weekday() >= 5:
        week_anchor = first_day + timedelta(days=7 - first_day.weekday())

    # meetings: the syllabus's, with SFU's times as a check and its dates as the range
    meetings = []
    for m in raw.get("meetings") or []:
        for day in m.get("days") or []:
            if day not in DAY_INDEX:
                continue
            meetings.append({"id": new_id(), "kind": m.get("kind") or "lecture", "day": DAY_INDEX[day],
                             "start": clean_time(m.get("start")), "end": clean_time(m.get("end")),
                             "location": m.get("location") or "", "section": m.get("section") or "",
                             "startDate": None, "endDate": None, "include": True, "sure": True, "why": None,
                             "source": "syllabus", "evidence": m.get("evidence") or ""})
    for om in off.get("meetings") or []:
        for day in om["days"]:
            match = next((x for x in meetings if x["kind"] == om["kind"] and x["day"] == day), None)
            if match:
                match["startDate"], match["endDate"] = om["startDate"], om["endDate"]
                match["source"] = "both"
                if om["start"] and match["start"] and (om["start"], om["end"]) != (match["start"], match["end"]):
                    match["sure"] = False
                    match["why"] = (f"The syllabus says {match['start']}-{match['end']}, "
                                    f"SFU's outline says {om['start']}-{om['end']}.")
                if not match["location"] and om["location"]:
                    match["location"] = om["location"]
            else:
                meetings.append({"id": new_id(), "kind": om["kind"], "day": day, "start": om["start"],
                                 "end": om["end"], "location": om["location"], "section": off.get("section", ""),
                                 "startDate": om["startDate"], "endDate": om["endDate"], "include": True,
                                 "sure": not any(x["kind"] == om["kind"] for x in meetings),
                                 "why": None, "source": "official", "evidence": "SFU official outline"})
    for m in meetings:
        if m["kind"] in ("tutorial", "lab") and len([x for x in meetings if x["kind"] == m["kind"]]) > 1:
            m["sure"] = False
            m["why"] = m["why"] or f"Several {m['kind']} times are listed. Keep only your own section."
        if not m["start"]:
            m["sure"], m["why"] = False, m["why"] or "No time given."
    meeting_days = sorted({m["day"] for m in meetings if m["kind"] == "lecture"})

    categories = []
    for g in raw.get("grade_categories") or []:
        categories.append({"id": new_id(), "name": g.get("name") or "Category", "weight": g.get("weight_percent"),
                           "dropLowest": max(0, g.get("drop_lowest") or 0), "count": g.get("item_count"),
                           "include": True, "evidence": g.get("evidence") or ""})
    by_name = {g["name"].strip().lower(): g for g in categories}

    items = []

    def add_item(a, due, sure, why, group=None, group_label=None, title=None, flag=None):
        cat = by_name.get((a.get("category") or "").strip().lower())
        if a.get("_year_fixed") and due:
            sure = False
            why = (f"The reading put this in {a['_year_fixed']}; moved to {due.year}, the course's year. "
                   + (why or "")).strip()
            flag = flag or "year"
        items.append({
            "id": new_id(), "title": title or a.get("title") or "Untitled", "type": a.get("type") or "assignment",
            "categoryId": cat["id"] if cat else None,
            "weight": None if cat else a.get("weight_percent"),
            "dueDate": due.isoformat() if due else None, "dueTime": clean_time(a.get("due_time")),
            "location": a.get("location") or "", "include": True, "sure": sure, "why": why,
            "evidence": a.get("evidence") or "", "dateAsWritten": a.get("date_as_written") or "",
            "group": group, "groupLabel": group_label, "source": "syllabus",
            "flag": None if sure else (flag or "model"),
        })

    for a in raw.get("assessments") or []:
        sure, why = bool(a.get("sure")), a.get("unsure_why")
        if a.get("repeats"):
            dates = expand_repeats(a["repeats"], week_anchor, last_day, no_class)
            if not dates:
                add_item(a, None, False, "Repeats, but Vesta could not work out the dates. Add them by hand.", flag="pattern")
                continue
            anchored = bool(to_date(a["repeats"].get("first_date")) and to_date(a["repeats"].get("last_date")))
            # "Reading responses 10%" is 10% shared by every response, not 10% each
            if a.get("weight_percent") and not by_name.get((a.get("category") or "").strip().lower()):
                cat = {"id": new_id(), "name": a.get("title") or "Repeating work", "weight": a["weight_percent"],
                       "dropLowest": 0, "count": len(dates), "include": True, "evidence": a.get("evidence") or ""}
                categories.append(cat)
                by_name[cat["name"].strip().lower()] = cat
                a = dict(a, category=cat["name"], weight_percent=None)
            gid = new_id()
            label = f"{a.get('title')}: every {'week' if (a['repeats'].get('every_weeks') or 1) == 1 else str(a['repeats']['every_weeks']) + ' weeks'} on {a['repeats'].get('weekday')}"
            for n, d in enumerate(dates, 1):
                d_sure = sure and anchored and d not in no_class
                d_why = why or (None if anchored else "Dates worked out from the term and the repeating pattern.")
                if d in no_class:
                    d_why = (d_why + " " if d_why else "") + "There is no class that day; check it is still due."
                add_item(a, d, d_sure, d_why, group=gid, group_label=label, title=f"{a.get('title')} {n}",
                         flag="noclass" if d in no_class else "pattern")
            continue
        due = to_date(a.get("due_date"))
        flag = None
        if not due and a.get("week"):
            flag = "week"
            due = resolve_week(week_anchor, a["week"], a.get("weekday"), meeting_days, avoid=no_class)
            sure = False
            why = why or (f"The syllabus gives Week {a['week']}; Vesta worked out the date"
                          + ("" if a.get("weekday") else " using the first class that week") + "."
                          if due else f"Week {a['week']}, but the term's start date is unknown.")
        if due and due in no_class:
            sure, why = False, (why + " " if why else "") + "That date is listed as a day with no class."
            flag = flag or "noclass"
        add_item(a, due, sure, why, flag=flag)

    # the final exam: SFU usually knows the slot the syllabus leaves as TBA
    fe = off.get("finalExam")
    if fe and fe.get("date"):
        finals = [i for i in items if i["type"] == "exam" and re.search(r"final", i["title"], re.I)]
        if finals and not finals[0]["dueDate"]:
            finals[0].update(dueDate=fe["date"].isoformat(), dueTime=fe["start"], location=finals[0]["location"] or fe["location"],
                             sure=True, why=None, source="official")
        elif finals and finals[0]["dueDate"] != fe["date"].isoformat():
            finals[0]["sure"] = False
            finals[0]["why"] = f"The syllabus date differs from SFU's exam schedule ({fe['date'].isoformat()} {fe['start'] or ''})."
        elif not finals:
            items.append({"id": new_id(), "title": "Final exam", "type": "exam", "categoryId": None, "weight": None,
                          "dueDate": fe["date"].isoformat(), "dueTime": fe["start"], "location": fe["location"],
                          "include": True, "sure": False, "why": "Found only in SFU's exam schedule, not the syllabus.",
                          "evidence": "SFU exam schedule", "dateAsWritten": "", "group": None, "groupLabel": None,
                          "source": "official", "flag": "model"})

    scale = sorted([{"letter": s["letter"].strip(), "min": s["min_percent"]} for s in raw.get("grade_scale") or []
                    if s.get("letter")], key=lambda s: -s["min"])
    total = sum(g["weight"] or 0 for g in categories) + sum(i["weight"] or 0 for i in items if not i["categoryId"])
    warnings = list(raw.get("warnings") or [])
    if from_filename:
        warnings.append("The " + " and ".join("course code" if k == "code" else "term" for k in from_filename)
                        + " came from the file name, not the document.")
    if years_moved:
        warnings.append(f"{years_moved} date{'s' if years_moved != 1 else ''} came back in the wrong year and "
                        f"{'were' if years_moved != 1 else 'was'} moved to {year}, where the weekdays still match.")
    # A scale that jumps from A- straight to F would mark an 81% as failing. That is
    # nearly always a scale Claude only partly read, so say so and do not apply it.
    scale_incomplete = False
    if scale:
        passing = [r["min"] for r in scale if r["min"] and r["min"] > 0]
        if passing and min(passing) > 65:
            scale_incomplete = True
            warnings.append(f"The letter-grade scale stops at {min(passing)}%, so anything lower would be an F. "
                            "Some letters are probably missing. Check it before using it.")
    if total and abs(total - 100) > 0.5:
        warnings.append(f"The weights add up to {round(total, 1)}%, not 100%. Check them against the syllabus.")

    return {
        "course": course, "courseChecks": conflicts, "meetings": meetings, "gradeScale": scale,
        "categories": categories, "items": sorted(items, key=lambda i: (i["dueDate"] or "9999", i["title"])),
        "topics": [{"id": new_id(), "week": t.get("week"), "date": t.get("date"), "title": t.get("title") or "",
                    "include": True} for t in raw.get("topics") or [] if t.get("title")],
        "noClassDates": [{"date": x.get("date"), "label": x.get("label") or ""} for x in raw.get("no_class_dates") or []],
        "firstDay": first_day.isoformat() if first_day else None,
        "lastDay": last_day.isoformat() if last_day else None,
        "warnings": warnings, "officialNote": official_note, "scaleIncomplete": scale_incomplete,
        "officialFound": bool(official),
    }

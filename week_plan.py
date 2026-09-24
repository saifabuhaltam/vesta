"""This Week from a course's own schedule, found and read automatically.

Every course Saif takes has a document that lays the term out week by week: IAT 201's
Course Progression Map, PHIL 110's syllabus (linked only from its Canvas Syllabus page),
REM 388's course outline, SD 381's "List of weeks, readings and assignments", PSYC
300W's syllabus. See WEEK.md, step 5 and "Automatic, 2026-09-24".

He did not want to find and submit these himself ("why can't it just go through my
course material ... and just autofill it accordingly"), so after every Canvas check:

1. **Find** the documents that might be a schedule, by name, among the course's module
   files and pages, the files its Syllabus page links, and the class's own files.
2. **Score** each one for free: how many distinct weeks of the term its text names, by
   date or by "Week N". A textbook or a policy page scores near zero and is never paid
   for (PHIL 110's syllabus page links a 300-page textbook).
3. **Read** the best one with AI, only when it is new or the professor changed it, and
   add what it says straight to This Week. No review step: that was his call.

Rows get ids built from what they say, so a re-read of an updated schedule keeps every
tick on the rows that did not change.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta

import ai
import week

STATE_PREFIX = "week_plan_auto:"
# Thinking counts toward max_tokens, and the configured model thinks unless told not
# to. At the default effort IAT 201's map used all of 16,000 before writing a word of
# the answer. This is transcription, not reasoning, so effort is low and the ceiling
# is the syllabus reader's.
PLAN_MAX_TOKENS = 32000
PLAN_EFFORT = "low"
EXPECTED_OUTPUT_TOKENS = 8000
MAX_INPUT_CHARS = 120000          # about 30,000 tokens; a whole syllabus fits easily
MAX_DOC_BYTES = 25 * 1024 * 1024  # a schedule is never this big; a textbook often is
# How many different weeks of the term a document must name to count as a schedule.
# His five schedules name 8 to 14; the textbook names none.
MIN_WEEKS = 5

KINDS = ["reading", "watch", "assignment", "quiz", "exam", "discussion", "start",
         "work", "note", "topic"]

# Names worth downloading to score. Only a name that might be a schedule is fetched.
NAME_RE = re.compile(r"schedule|progression|syllabus|outline|course map|calendar|"
                     r"week[- ]by[- ]week|list of weeks|timeline|reading list|course info", re.I)
STRONG_RE = re.compile(r"schedule|progression|syllabus|course map|week[- ]by[- ]week|list of weeks", re.I)


STR = {"type": "string"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["date", "week", "kind", "title", "detail"],
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD, the day this belongs to; empty string if the document gives no date"},
                    "week": {"type": "integer", "description": "the document's own week number; 0 if none"},
                    "kind": {"type": "string", "enum": KINDS},
                    "title": STR,
                    "detail": {"type": "string", "description": "one short sentence, or empty string"},
                },
            },
        },
    },
}

SYSTEM = """You turn a university course's week-by-week plan into a student's weekly checklist.

Return every separate thing the document asks the student to do, in order, each placed on a day:

- `reading`: one item per reading. Title starts with "Read", then what to read in the
  document's words ("Read Ch. 3: Truth tables", "Read Noba: Attention, and Failures of
  Awareness"). Date: the day of the week it is assigned for (the class or lecture day
  the document gives for that week).
- `watch`: lectures or videos to watch.
- `assignment`, `quiz`, `exam`, `discussion`: on the day it is due or written. Title as
  the document names it. `detail`: points, weight or where it happens, if stated.
- `start`: only where the document itself says to begin something early ("start
  studying", "start the first draft"). Title starts with "Start".
- `work`: other things to do that week (visit an archive, prepare a presentation).
- `note`: notices a student must know that are not tasks ("No section: National Day for
  Truth and Reconciliation", "Class cancelled: Thanksgiving").
- `topic`: one per week, the week's topic, on that week's date.

Rules:
- Dates are YYYY-MM-DD. The term and year are given below; use the document's own
  dates. If it gives a week number and a weekday but no date, set `week` and leave
  `date` empty. Never guess a date the document does not support.
- Use the document's words. Do not add readings, deadlines or advice it does not give.
- List each reading once. Documents often name a reading twice: as homework "for next
  week" in one week, and again in the week it belongs to, sometimes once by author and
  once by title. Keep only the second, in the week it belongs to.
- Skip course policies, grading schemes, contact details and anything not tied to a week.
- Keep titles under 90 characters. Put anything longer in `detail`."""


# ---------------------------------------------------------------------------
# which document
# ---------------------------------------------------------------------------

def estimate(conn, text):
    cfg = ai.settings(conn)
    in_tokens = ai.estimate_tokens(SYSTEM) + ai.estimate_tokens(text) + 200
    # The answer grows with the document: IAT 201's map (7,100 tokens in) came back at
    # 4,200, PHIL 110's syllabus (3,800 in) at 1,400. A flat guess tripled the price
    # shown for the short one.
    out_tokens = min(EXPECTED_OUTPUT_TOKENS, max(1500, int(in_tokens * 0.6)))
    usd = ai.estimate_cost(cfg, in_tokens, out_tokens)
    return {"estimateUsd": round(usd, 3), "inputTokens": in_tokens, "model": cfg["model"],
            "spentToday": ai.spent_today(conn, cfg)["usd"], "dailyCap": cfg["daily_cap_usd"]}


def read_plan(conn, text, title, term, confirmed, client=None):
    """One paid call. Refuses before spending when unconfirmed or over the day's cap."""
    import anthropic

    cfg = ai.settings(conn)
    est = estimate(conn, text)
    if cfg["daily_cap_usd"] and est["spentToday"] + est["estimateUsd"] > cfg["daily_cap_usd"]:
        raise ai.AiRefused(dict(est, error="Reading this would go past today's AI limit.",
                                reason="daily_cap"), 402)
    # It runs on its own, for every account, on one key: the ceiling nobody can raise
    # from the app applies here as it does to every other call.
    everyone = ai.spent_today_everyone(cfg)
    if ai.GLOBAL_CAP_USD and everyone is not None and everyone >= ai.GLOBAL_CAP_USD:
        raise ai.AiRefused({"error": "Vesta has reached today's AI limit across all accounts.",
                            "reason": "global_cap"}, 402)
    if not confirmed:
        raise ai.AiRefused(dict(est, error="Confirm before Vesta reads the document.",
                                reason="confirm"), 409)
    context = ("Term: %s, first day of classes %s, last day %s."
               % (term.get("name") or "", term.get("start_date") or "unknown",
                  term.get("end_date") or "unknown"))
    request = dict(
        model=cfg["model"], max_tokens=PLAN_MAX_TOKENS, system=SYSTEM,
        output_config={"effort": PLAN_EFFORT,
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "The document, \"%s\":\n\n%s" % (title, text[:MAX_INPUT_CHARS])},
            {"type": "text", "text": context + "\n\nMake the weekly checklist."},
        ]}],
    )
    client = client or anthropic.Anthropic()
    try:
        with client.messages.stream(**request) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise ai.AiRefused({"error": "The Anthropic API key is missing or was rejected."}, 503)
    except anthropic.RateLimitError:
        raise ai.AiRefused({"error": "Anthropic is rate limiting right now. Wait a minute and try again."}, 429)
    except anthropic.APIConnectionError:
        raise ai.AiRefused({"error": "Could not reach the Anthropic API."}, 502)
    except anthropic.APIStatusError as e:
        raise ai.AiRefused({"error": "Anthropic error: %s" % e.message}, 502)
    usage = msg.usage
    ai.record_usage(conn, "week_plan", msg.model, usage.input_tokens, usage.output_tokens)
    if msg.stop_reason == "refusal":
        raise ai.AiRefused({"error": "The model declined to read this document."}, 422)
    if msg.stop_reason == "max_tokens":
        raise ai.AiRefused({"error": "The document was too long to read in one go."}, 422)
    raw = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ai.AiRefused({"error": "The reading came back malformed. Try again."}, 502)
    cost = ai.estimate_cost(cfg, usage.input_tokens, usage.output_tokens, model=msg.model)
    return data, {"model": msg.model, "costUsd": round(cost, 4)}




# ---------------------------------------------------------------------------
# is it a schedule? (free)
# ---------------------------------------------------------------------------

def schedule_weeks(text, term_start, term_end=None):
    """How many different weeks of the term a text names, by date or as "Week N".

    The free test that decides what is worth paying to read. Measured on his real
    documents: the five schedules name 8 to 14 weeks; PHIL 110's textbook names none.
    """
    if not text or not term_start:
        return 0
    end = term_end or term_start + timedelta(days=130)
    weeks = set()
    for d in week.dates_in(text, term_start):
        if term_start - timedelta(days=7) <= d <= end + timedelta(days=21):
            weeks.add(week.week_number(d, term_start))
    for m in week.WEEK_RE.finditer(text):
        n = int(m.group(1))
        if 1 <= n <= 16:
            weeks.add(n)
    return len(weeks)


def _weeks_of(c, term_start, term_end=None):
    if c.get("text") is None and c.get("weeks") is not None:
        return c["weeks"]
    return schedule_weeks(c.get("text"), term_start, term_end)


def _name_rank(name):
    return 0 if STRONG_RE.search(name or "") else 1


def choose(candidates, term_start, term_end=None):
    """The candidate most likely to be the course's schedule, or None.

    Each candidate is {"key", "title", "text", "fingerprint"}. Ranked by how many weeks
    it covers, then by a name that says schedule, then by length (a fuller outline over
    a one-page summary).
    """
    best, best_rank = None, None
    for c in candidates:
        n = _weeks_of(c, term_start, term_end)
        if n < MIN_WEEKS:
            continue
        rank = (n, -_name_rank(c.get("title")), len(c.get("text") or ""))
        if best_rank is None or rank > best_rank:
            best, best_rank = dict(c, weeks=n), rank
    return best


# ---------------------------------------------------------------------------
# finding candidates
# ---------------------------------------------------------------------------

SYLLABUS_FILE_RE = re.compile(r"/files/(\d+)")


def syllabus_file_ids(html):
    """File ids linked from a Canvas Syllabus page, in order, without repeats."""
    seen, out = set(), []
    for m in SYLLABUS_FILE_RE.finditer(html or ""):
        fid = m.group(1)
        if fid not in seen:
            seen.add(fid)
            out.append(fid)
    return out


def canvas_refs(modules, syllabus_html):
    """Which Canvas files and pages might be a schedule, from the stored modules and the
    Syllabus page. Nothing is downloaded here."""
    refs, seen = [], set()
    for m in modules or []:
        for it in m.get("items") or []:
            title = it.get("title") or ""
            if not NAME_RE.search(title):
                continue
            if it.get("type") == "File" and it.get("content_id"):
                key = "file:%s" % it["content_id"]
            elif it.get("type") == "Page" and it.get("page_url"):
                key = "page:%s" % it["page_url"]
            else:
                continue
            if key not in seen:
                seen.add(key)
                refs.append({"key": key, "title": title})
    for fid in syllabus_file_ids(syllabus_html):
        key = "file:%s" % fid
        if key not in seen:
            seen.add(key)
            refs.append({"key": key, "title": ""})
    return refs


def canvas_candidates(client, course_id, modules, extract_text, known=None):
    """Canvas documents that might be a schedule, with their text or a way to get it.

    `known` maps a fingerprint (a file's id, size and modified time) to the score it got
    last time. A file already scored is not downloaded again just to be scored: it
    comes back with its score and a `load` that fetches the text only if it is chosen
    and has to be read. That keeps a daily check to one metadata call per file.
    """
    import canvas
    known = known or {}
    try:
        course = client._get(client.base + "/courses/%s" % course_id,
                             {"include[]": "syllabus_body"}).json()
    except canvas.CanvasError:
        course = {}
    body = course.get("syllabus_body") or ""
    out = []
    if body:
        text = canvas.plain_text(body)
        out.append({"key": "canvas-syllabus:%s" % course_id, "title": "Canvas Syllabus page",
                    "text": text, "fingerprint": _digest("syl", text)})
    for ref in canvas_refs(modules, body):
        try:
            if ref["key"].startswith("file:"):
                f = client.file(ref["key"].split(":", 1)[1])
                name = f.get("display_name") or ref["title"] or "file"
                if (f.get("size") or 0) > MAX_DOC_BYTES or re.search(
                        r"\.(png|jpe?g|gif|mp4|mov|zip)$", name, re.I):
                    continue
                fp = _digest("file", f.get("id"), f.get("size"), f.get("modified_at") or f.get("updated_at"))
                cand = {"key": "canvas-file:%s" % f.get("id"), "title": name, "fingerprint": fp}
                if fp in known:
                    cand.update(text=None, weeks=known[fp],
                                load=lambda f=f: _download_text(client, f, extract_text))
                else:
                    cand["text"] = _download_text(client, f, extract_text)
                out.append(cand)
            else:
                slug = ref["key"].split(":", 1)[1]
                page = client._get(client.base + "/courses/%s/pages/%s" % (course_id, slug)).json()
                fp = _digest("page", slug, page.get("updated_at"))
                text = canvas.plain_text(page.get("body") or "")
                out.append({"key": "canvas-page:%s" % slug,
                            "title": page.get("title") or ref["title"],
                            "text": text, "fingerprint": fp})
        except canvas.CanvasError as e:
            # One unreadable file must not stop the rest. A dead token or a throttle
            # will fail everything after it, so it does stop.
            if e.needs_token or e.rate_limited:
                raise
            continue
    return out


def material_candidates(conn, class_id):
    """Files already in the class whose name might be a schedule, with their text.

    What a class that is not on Canvas gets, which is the case for a friend whose
    school does not use it: upload the syllabus, and the week fills in.
    """
    out = []
    for r in conn.execute(
            "SELECT id, title, filename, extracted_text FROM materials WHERE class_id=?"
            " AND extracted_text IS NOT NULL AND extracted_text <> ''", (class_id,)).fetchall():
        name = r["title"] or r["filename"] or ""
        if not NAME_RE.search(name) and not NAME_RE.search(r["filename"] or ""):
            continue
        text = r["extracted_text"] or ""
        out.append({"key": "material:%s" % r["id"], "title": name, "text": text,
                    "fingerprint": _digest("mat", r["id"], len(text), text[:2000])})
    return out


def _download_text(client, f, extract_text):
    name = re.sub(r"[^\w.\- ]+", "_", f.get("display_name") or "file") or "file"
    tmp = tempfile.mkdtemp(prefix="vesta-plan-")
    try:
        path = os.path.join(tmp, name)
        client.download(f["url"], path, max_bytes=MAX_DOC_BYTES)
        return (extract_text(path, name) or "").strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _digest(*parts):
    raw = json.dumps(parts, default=str, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

def items_from(data, term_start):
    """The model's items, each placed on a day.

    An item with only a week number is placed on that week's Monday by Vesta's own
    numbering. One with neither is dropped: a to-do with no week cannot appear in any.
    """
    items = []
    for it in (data or {}).get("items") or []:
        title = (it.get("title") or "").strip()[:200]
        kind = it.get("kind") if it.get("kind") in KINDS else "work"
        if not title:
            continue
        day = week.parse_iso(it.get("date"))
        if not day and it.get("week") and term_start:
            day = week.week_start(int(it["week"]), term_start)
        if not day:
            continue
        items.append({"day": day.isoformat(), "kind": kind, "title": title,
                      "detail": (it.get("detail") or "").strip()[:300]})
    items.sort(key=lambda i: (i["day"], KINDS.index(i["kind"])))
    return items


def row_id(class_id, item):
    """An id built from what the row says, so an unchanged row keeps its tick when an
    updated schedule is read again."""
    return _digest(class_id, item["day"], item["kind"], week.canvas_norm(item["title"]))


def replace_plan(conn, class_id, items, source):
    """The class's plan becomes `items`. Ticks on rows that no longer exist go too."""
    now = datetime.utcnow().isoformat()
    old = {r["id"] for r in conn.execute("SELECT id FROM week_plan_items WHERE class_id=?",
                                         (class_id,)).fetchall()}
    conn.execute("DELETE FROM week_plan_items WHERE class_id=?", (class_id,))
    new, seen = set(), set()
    for order, it in enumerate(items):
        rid = row_id(class_id, it)
        if rid in seen:
            continue
        seen.add(rid)
        new.add(rid)
        conn.execute(
            "INSERT INTO week_plan_items (id, class_id, day, kind, title, detail, source,"
            " sort_order, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, class_id, it["day"], it["kind"], it["title"], it.get("detail") or "",
             source, order, now))
    for gone in old - new:
        conn.execute("DELETE FROM week_marks WHERE key=?", ("plan:%s" % gone,))
    return len(new)


def clear_plan(conn, class_id):
    ids = [r["id"] for r in conn.execute("SELECT id FROM week_plan_items WHERE class_id=?",
                                         (class_id,)).fetchall()]
    conn.execute("DELETE FROM week_plan_items WHERE class_id=?", (class_id,))
    for pid in ids:
        conn.execute("DELETE FROM week_marks WHERE key=?", ("plan:%s" % pid,))


def plan_rows(conn, class_ids, include_off=False):
    """Every class's stored plan, as {class id: [rows]}. A class he switched to Canvas
    modules only keeps its plan, but it is left out here unless asked for."""
    out = {}
    if not class_ids:
        return out
    marks = ",".join("?" * len(class_ids))
    for r in conn.execute(
            "SELECT id, class_id, day, kind, title, detail, source, sort_order FROM week_plan_items"
            " WHERE class_id IN (%s) ORDER BY day, sort_order" % marks, list(class_ids)).fetchall():
        out.setdefault(r["class_id"], []).append(dict(r))
    if not include_off:
        for cid in list(out):
            if load_state(conn, cid).get("off"):
                del out[cid]
    return out


# ---------------------------------------------------------------------------
# per-class state
# ---------------------------------------------------------------------------

def load_state(conn, class_id):
    import db
    raw = db.get_setting(conn, STATE_PREFIX + class_id, "")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def save_state(conn, class_id, state):
    import db
    db.set_setting(conn, STATE_PREFIX + class_id, json.dumps(state))


def public_state(state):
    return {k: state.get(k) for k in ("source", "sourceTitle", "readAt", "costUsd",
                                      "error", "off", "pinned", "weeks", "checkedAt")}


# ---------------------------------------------------------------------------
# the automatic pass
# ---------------------------------------------------------------------------

EXTRACT_TEXT = None      # set by app.py: extract_text(path, filename)


def plan_class(conn, class_id, candidates, term, read=None):
    """Choose, and read if it changed, the schedule for one class. Returns what happened.

    `read` is `read_plan` unless a test passes a stand-in. Never raises for an AI
    refusal: the reason is stored where the page shows it, and the next pass tries
    again, which is what a background job with nobody watching has to do.
    """
    read = read or read_plan
    state = load_state(conn, class_id)
    state["checkedAt"] = datetime.utcnow().isoformat()
    if state.get("off"):
        save_state(conn, class_id, state)
        return "off"
    term_start = week.parse_iso(term.get("start_date"))
    term_end = week.parse_iso(term.get("end_date"))
    pinned = state.get("pinned")
    state["scores"] = {c["fingerprint"]: _weeks_of(c, term_start, term_end) for c in candidates}
    if pinned:
        chosen = next((dict(c, weeks=_weeks_of(c, term_start, term_end))
                       for c in candidates if c["key"] == pinned), None)
    else:
        chosen = choose(candidates, term_start, term_end)
    if chosen is None:
        save_state(conn, class_id, state)
        return "none"
    if chosen["fingerprint"] == state.get("fingerprint"):
        save_state(conn, class_id, state)
        return "unchanged"
    if chosen.get("text") is None and chosen.get("load"):
        chosen["text"] = chosen["load"]()
    try:
        data, meta = read(conn, chosen["text"], chosen["title"], term, True)
    except ai.AiRefused as e:
        state["error"] = e.payload.get("error") or "The schedule could not be read."
        save_state(conn, class_id, state)
        return "refused"
    items = items_from(data, term_start)
    if not items:
        # Read and paid for, and nothing in it is tied to a week. Remembered, so it is
        # not paid for again until it changes.
        state.update({"fingerprint": chosen["fingerprint"], "error":
                      "%s has no week-by-week plan in it." % chosen["title"]})
        save_state(conn, class_id, state)
        return "empty"
    replace_plan(conn, class_id, items, chosen["title"])
    state.update({"source": chosen["key"], "sourceTitle": chosen["title"],
                  "fingerprint": chosen["fingerprint"], "readAt": datetime.utcnow().isoformat(),
                  "costUsd": meta.get("costUsd"), "weeks": chosen.get("weeks"), "error": None})
    save_state(conn, class_id, state)
    conn.commit()
    return "read"


def auto_plan(user_id, class_ids=None):
    """After a Canvas check: plan every live class from its schedule. Background-safe.

    Classes on Canvas are searched there and in their own files; a class that is not is
    searched in its own files only. Each class runs on its own connection, so one
    course's failure is recorded and the rest carry on.
    """
    import canvas
    import canvas_sync
    import db

    conn = db.get_db(user_id=user_id)
    try:
        term = dict(db.active_semester(conn))
        cstate = canvas_sync.load_state(conn)
        mapped = {e.get("classId"): cid for cid, e in (cstate.get("courses") or {}).items()
                  if e.get("classId")}
        rows = conn.execute("SELECT id FROM classes WHERE semester_id=?", (term["id"],)).fetchall()
        targets = [r["id"] for r in rows if class_ids is None or r["id"] in class_ids]
        client = None
        if (cstate.get("token") or "").strip():
            try:
                client = canvas.Client(cstate.get("host"), cstate.get("token"))
            except canvas.CanvasError:
                client = None
    finally:
        conn.close()

    def one(class_id):
        conn = db.get_db(user_id=user_id)
        try:
            if not canvas_sync.class_is_live(conn, class_id):
                return None
            cands = material_candidates(conn, class_id)
            course_id = mapped.get(class_id)
            if client is not None and course_id and EXTRACT_TEXT:
                snap = canvas_sync.load_snapshot(conn, course_id) or {}
                known = load_state(conn, class_id).get("scores") or {}
                try:
                    cands = canvas_candidates(client, course_id, snap.get("modules") or [],
                                              EXTRACT_TEXT, known) + cands
                except canvas.CanvasError as e:
                    return "canvas: %s" % e.message
            result = plan_class(conn, class_id, cands, term)
            conn.commit()
            return result
        except Exception as e:          # one class must never take the rest down
            return "failed: %s" % e
        finally:
            conn.close()

    # Side by side: a first pass reads five schedules at about half a minute each, and
    # one after another took three minutes, longer than the page waits for a check.
    from concurrent.futures import ThreadPoolExecutor
    report = {}
    with ThreadPoolExecutor(max_workers=max(1, min(5, len(targets)))) as pool:
        for class_id, result in zip(targets, pool.map(one, targets)):
            if result is not None:
                report[class_id] = result
    return report


# ---------------------------------------------------------------------------
# classes that are not on Canvas
# ---------------------------------------------------------------------------

_inflight = set()
_inflight_guard = None


def needs_plan(conn, class_id):
    """A class off Canvas has a schedule-like file this pass has not scored yet.

    Free to ask: it compares fingerprints of the class's own files with the ones the
    last pass saw, and reads nothing.
    """
    state = load_state(conn, class_id)
    if state.get("off"):
        return False
    seen = set((state.get("scores") or {}).keys()) | {state.get("fingerprint")}
    return any(c["fingerprint"] not in seen for c in material_candidates(conn, class_id))


def plan_soon(user_id, class_ids):
    """Plan these classes in the background, once each at a time. Returns those started.

    Canvas classes are planned after every Canvas check. A class that is not on Canvas
    has no check to hang this on, so opening This Week starts it instead.
    """
    global _inflight_guard
    import threading
    if _inflight_guard is None:
        _inflight_guard = threading.Lock()
    started = []
    with _inflight_guard:
        for cid in class_ids:
            if (user_id, cid) not in _inflight:
                _inflight.add((user_id, cid))
                started.append(cid)
    if not started:
        return [cid for cid in class_ids if (user_id, cid) in _inflight]

    def run():
        try:
            auto_plan(user_id, class_ids=started)
        finally:
            with _inflight_guard:
                for cid in started:
                    _inflight.discard((user_id, cid))
    threading.Thread(target=run, daemon=True).start()
    return list(class_ids)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

from flask import Blueprint, jsonify, request  # noqa: E402

bp = Blueprint("week_plan", __name__)


def _candidates_for(conn, class_id):
    """Everything this class could be planned from, for the "Wrong document?" list."""
    import canvas
    import canvas_sync
    cstate = canvas_sync.load_state(conn)
    course_id = next((cid for cid, e in (cstate.get("courses") or {}).items()
                      if e.get("classId") == class_id), None)
    cands = material_candidates(conn, class_id)
    if course_id and (cstate.get("token") or "").strip() and EXTRACT_TEXT:
        try:
            client = canvas.Client(cstate.get("host"), cstate.get("token"))
            snap = canvas_sync.load_snapshot(conn, course_id) or {}
            cands = canvas_candidates(client, course_id, snap.get("modules") or [],
                                      EXTRACT_TEXT) + cands
        except canvas.CanvasError:
            pass
    return cands


@bp.route("/api/week/plan/<class_id>", methods=["GET"])
def get_plan(class_id):
    """Where this class's plan came from, and, with ?sources=1, what else it could use."""
    import db
    conn = db.get_db()
    try:
        state = load_state(conn, class_id)
        out = {"state": public_state(state)}
        if request.args.get("sources") == "1":
            term = dict(db.active_semester(conn))
            ts, te = week.parse_iso(term.get("start_date")), week.parse_iso(term.get("end_date"))
            out["sources"] = sorted(
                ({"key": c["key"], "title": c["title"],
                  "weeks": schedule_weeks(c["text"], ts, te),
                  "chosen": c["key"] == state.get("source")}
                 for c in _candidates_for(conn, class_id)),
                key=lambda s: (-s["weeks"], s["title"].lower()))
        return jsonify(out)
    finally:
        conn.close()


@bp.route("/api/week/plan/<class_id>/use", methods=["POST"])
def use_source(class_id):
    """He says the automatic choice was wrong: read this document instead, and keep
    using it. Takes {source}; {source: null} goes back to choosing automatically."""
    import db
    body = request.get_json(silent=True) or {}
    key = body.get("source")
    conn = db.get_db()
    try:
        state = load_state(conn, class_id)
        state.update({"pinned": key or None, "off": False, "fingerprint": None, "error": None})
        save_state(conn, class_id, state)
        cands = _candidates_for(conn, class_id)
        if key and not any(c["key"] == key for c in cands):
            return jsonify({"error": "That document is no longer there."}), 404
        result = plan_class(conn, class_id, cands, dict(db.active_semester(conn)))
        conn.commit()
        return jsonify({"result": result, "state": public_state(load_state(conn, class_id))})
    finally:
        conn.close()


@bp.route("/api/week/plan/<class_id>/off", methods=["POST"])
def set_off(class_id):
    """Use Canvas modules only for this class ({off: true}), or go back ({off: false})."""
    import db
    body = request.get_json(silent=True) or {}
    off = bool(body.get("off"))
    conn = db.get_db()
    try:
        state = load_state(conn, class_id)
        state["off"] = off
        # The plan and its ticks are kept while it is off, only not shown, so turning
        # it back on is instant and does not pay to read the same document again.
        save_state(conn, class_id, state)
        result = None
        kept = conn.execute("SELECT 1 FROM week_plan_items WHERE class_id=? LIMIT 1",
                            (class_id,)).fetchone()
        if not off and not kept:
            # Nothing kept to show (it was never planned, or the plan was cleared), so
            # look for a schedule now rather than waiting for the next Canvas check.
            result = plan_class(conn, class_id, _candidates_for(conn, class_id),
                                dict(db.active_semester(conn)))
        conn.commit()
        return jsonify({"ok": True, "result": result})
    finally:
        conn.close()

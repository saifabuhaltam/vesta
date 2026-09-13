"""Pulling a course's real schedule from SFU and drafting it for review.

This is the half of calendar sync that needs no account and no credentials: SFU
publishes every section's meeting pattern, room and final exam for free, and Vesta
already speaks that API for syllabus import.

Nothing here writes to the database. `schedule_draft` returns the same shape the
syllabus review screen already renders, so an imported timetable gets checked by the
student exactly like an imported syllabus does, rather than appearing unannounced.
"""
import hashlib
import re
import uuid

import syllabus as S

# SFU's own section vocabulary. `classType` "e" is a section you enrol in and that
# carries the course; "n" hangs off one of those (the lab or tutorial you also attend).
ENROLMENT = "e"
KIND_LABELS = {"lecture": "Lecture", "lab": "Lab", "tutorial": "Tutorial", "seminar": "Seminar"}
DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def hhmm(t):
    """SFU says '9:30'; Vesta stores '09:30'. Unpadded times compare unequal and sort wrong."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", t or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def same_course(a, b):
    """'IAT 201' and 'iat-201 B100' are one course; 'COGS 300' is not."""
    x, y = S.split_code(a or ""), S.split_code(b or "")
    if not x[0] or not y[0]:
        return True                       # unparseable on either side: do not block on a guess
    return x == y


def meeting_hash(kind, day, start, end, location):
    raw = "␟".join([kind or "", str(day), start or "", end or "", (location or "").strip().lower()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Which section is yours
# ---------------------------------------------------------------------------
def section_options(code, term, fetch=S.fetch_json):
    """The sections of a course, grouped so a picker can ask two questions at most.

    A course is either several independent lectures (CMPT 120: D100/D200/D300) or one
    lecture with labs hanging off it (IAT 201: B100 plus B101-B104). `associatedClass`
    is what tells those apart, so the labs offered are only ever the ones attached to
    the lecture actually chosen.
    """
    dept, number = S.split_code(code)
    t = S.split_term(term)
    if not dept or not t:
        return {"error": "Vesta could not work out the course code and term.", "primary": []}
    try:
        rows = fetch(f"{S.SFU_BASE}?{t[0]}/{t[1]}/{dept}/{number}")
    except Exception:
        return {"error": f"SFU has not published {code} for {term}.", "primary": []}
    if not isinstance(rows, list) or not rows:
        return {"error": f"SFU lists no sections for {code} in {term}.", "primary": []}

    def entry(r):
        kind = S.SFU_KINDS.get((r.get("sectionCode") or "").upper(), "lecture")
        return {"value": r.get("value"), "label": r.get("text") or (r.get("value") or "").upper(),
                "title": r.get("title") or "", "kind": kind,
                "kindLabel": KIND_LABELS.get(kind, kind.title()),
                "sectionCode": (r.get("sectionCode") or "").upper(),
                "enrolment": (r.get("classType") or "") == ENROLMENT,
                "group": str(r.get("associatedClass") or "")}

    entries = [entry(r) for r in rows if isinstance(r, dict) and r.get("value")]
    # If SFU flags none of them as enrolment sections, offer all of them rather than
    # showing an empty picker.
    primary = [e for e in entries if e["enrolment"]] or entries
    chosen = {e["value"] for e in primary}
    secondary = {}
    for e in entries:
        if e["value"] in chosen:
            continue
        secondary.setdefault(e["group"], []).append(e)
    return {"error": None, "primary": primary, "secondary": secondary,
            "path": f"{t[0]}/{t[1]}/{dept}/{number}",
            # a single lecture and no labs is not worth asking about
            "needsChoice": len(primary) > 1 or any(secondary.values())}


# ---------------------------------------------------------------------------
# The draft
# ---------------------------------------------------------------------------
def _outline(path, fetch):
    try:
        return S.parse_sfu_outline(fetch(f"{S.SFU_BASE}?{path}")), None
    except Exception:
        return None, f"SFU has not published an outline for {path.split('/')[-1].upper()} yet."


def schedule_draft(conn, class_id, code, term, sections, fetch=S.fetch_json):
    """One reviewable draft of a course's meetings and final, from SFU's own outline.

    `sections` is the list of section codes the student picked: their lecture, and any
    lab or tutorial. Each is fetched separately because SFU publishes one outline per
    section, and a lab's room is not in the lecture's.
    """
    dept, number = S.split_code(code)
    t = S.split_term(term)
    if not dept or not t:
        return {"error": "Vesta could not work out the course code and term."}
    base = f"{t[0]}/{t[1]}/{dept}/{number}"

    existing, mismatch = [], None
    if class_id:
        row = conn.execute("SELECT code FROM classes WHERE id=?", (class_id,)).fetchone()
        # Only reconcile against a class that really is this course. Drafting IAT 201
        # into a class called COGS 300 must not match COGS 300's meetings, and must
        # never offer to delete the lectures that class actually has.
        if row is not None and same_course(row["code"], code):
            existing = conn.execute(
                "SELECT * FROM schedule_entries WHERE class_id=?", (class_id,)).fetchall()
        elif row is not None and (row["code"] or "").strip():
            mismatch = (f"This class is {row['code']}, but the schedule below is for {code}. "
                        "Nothing already on the class will be changed or removed.")
    used, meetings, warnings, final_exam = set(), [], [], None
    if mismatch:
        warnings.append(mismatch)
    course, first_day, last_day = {}, None, None

    for sec in (sections or []):
        parsed, err = _outline(f"{base}/{str(sec).strip().lower()}", fetch)
        if err:
            warnings.append(err)
            continue
        if not course:
            course = {"code": parsed["code"], "name": parsed["title"], "section": parsed["section"],
                      "term": parsed["term"], "professor": parsed["instructor"],
                      "professorEmail": parsed["instructorEmail"]}
        first_day = min([d for d in (first_day, parsed["firstDay"]) if d], default=None)
        last_day = max([d for d in (last_day, parsed["lastDay"]) if d], default=None)

        for m in parsed["meetings"]:
            start, end = hhmm(m["start"]), hhmm(m["end"])
            if not m["days"]:
                warnings.append(
                    f"{str(sec).upper()} has no meeting days published, so nothing was added for it.")
                continue
            for day in m["days"]:
                match = next((e for e in existing if e["id"] not in used
                              and (e["kind"] or "lecture") == m["kind"] and e["day"] == day
                              and hhmm(e["start"]) == start), None)
                if match:
                    used.add(match["id"])
                changed = bool(match) and (
                    hhmm(match["end"]) != end or (match["location"] or "") != (m["location"] or ""))
                meetings.append({
                    "id": str(uuid.uuid4()), "kind": m["kind"], "day": day,
                    "start": start, "end": end, "location": m["location"] or "",
                    "section": str(sec).upper(),
                    "startDate": m["startDate"].isoformat() if m["startDate"] else None,
                    "endDate": m["endDate"].isoformat() if m["endDate"] else None,
                    "existingId": match["id"] if match else None,
                    "change": "same" if (match and not changed) else "changed" if match else "new",
                    # SFU is the registrar: its meeting pattern is not a guess
                    "sure": True, "why": "", "include": not (match and not changed),
                    "hash": meeting_hash(m["kind"], day, start, end, m["location"]),
                })

        # every section of a course shares one final, so the first one found is it
        if parsed["finalExam"] and parsed["finalExam"].get("date"):
            final_exam = final_exam or parsed["finalExam"]

    items = []
    final = final_exam
    if final:
        existing_exam = None
        if class_id:
            existing_exam = conn.execute(
                "SELECT * FROM items WHERE class_id=? AND type='exam' AND due_date=?",
                (class_id, final["date"].isoformat())).fetchone()
        items.append({
            "id": str(uuid.uuid4()), "title": "Final exam", "type": "exam",
            "dueDate": final["date"].isoformat(), "dueTime": hhmm(final.get("start")),
            "location": final.get("location") or "",
            "existingId": existing_exam["id"] if existing_exam else None,
            "change": "same" if existing_exam else "new",
            "sure": True, "why": "", "include": not existing_exam,
        })
    elif sections:
        warnings.append("SFU has not published a final exam time for this course yet.")

    gone = [{"existingId": e["id"],
             "what": f"{KIND_LABELS.get(e['kind'] or 'lecture', 'Meeting')} "
                     f"{DOW[e['day']] if e['day'] is not None and 0 <= e['day'] < 7 else ''} "
                     f"{hhmm(e['start']) or ''}".strip(),
             "include": False}
            for e in existing if e["id"] not in used]

    return {
        "error": None, "source": "sfu",
        "label": f"{course.get('code') or code} {'/'.join(str(s).upper() for s in sections)} · {term}",
        "course": course, "meetings": meetings, "items": items, "removed": gone,
        "firstDay": first_day.isoformat() if first_day else None,
        "lastDay": last_day.isoformat() if last_day else None,
        "warnings": [w for w in warnings if w],
        "classId": class_id,
    }

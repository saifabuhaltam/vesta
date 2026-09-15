"""HTTP side of syllabus import: upload, read, review, compare, import.

Nothing touches the student's classes until POST /api/syllabus/<id>/import, and that
refuses while any included item still needs a look. Re-importing into an existing
class only ever changes dates, weights and details the student ticked; scores,
completion and subtasks are never written.
"""
import json
import os
import re
import shutil
import uuid
from datetime import datetime

from flask import Blueprint, abort, current_app, jsonify, request
from werkzeug.utils import secure_filename

import ai
import syllabus as S
from db import (UPLOAD_DIR, get_db, active_semester, active_semester_id, semester_for,
                folder_id_for_kind)

bp = Blueprint("syllabus_import", __name__)

READABLE = ("pdf", "docx", "txt", "md", "png", "jpg", "jpeg", "webp", "gif", "html", "htm")


def now():
    return datetime.utcnow().isoformat()


def import_key(title, typ):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip() + "|" + (typ or "")


def load(conn, sid):
    row = conn.execute("SELECT * FROM syllabus_imports WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    return row


def summary(row, draft=None):
    return {"id": row["id"], "classId": row["class_id"], "filename": row["filename"], "status": row["status"],
            "model": row["model"], "createdAt": row["created_at"],
            "draft": draft if draft is not None else (json.loads(row["draft"]) if row["draft"] else None)}


# ---------------------------------------------------------------------------
# 1. Upload: store the file, price the read, spend nothing
# ---------------------------------------------------------------------------
@bp.route("/api/syllabus", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Choose the syllabus file first."}), 400
    original = secure_filename(f.filename) or "syllabus"
    if S.ext_of(original) not in READABLE:
        return jsonify({"error": "Vesta can read PDF, Word, text files and photos of a syllabus."}), 400
    sid = str(uuid.uuid4())
    stored = f"syllabus_{sid}_{original}"
    path = os.path.join(UPLOAD_DIR, stored)
    f.save(path)
    extract = current_app.config["EXTRACT_TEXT"]
    conn = get_db()
    try:
        text = extract(path, original) if S.ext_of(original) not in ("pdf",) + tuple(S.IMAGE_TYPES) else None
        _, tokens = S.document_block(path, original, text)
    except ai.AiRefused as e:
        conn.close()
        os.remove(path)
        return jsonify(e.payload), e.status
    class_id = request.form.get("classId") or None
    conn.execute("INSERT INTO syllabus_imports (id, class_id, filename, stored_name, mimetype, status, created_at)"
                 " VALUES (?,?,?,?,?,?,?)", (sid, class_id, original, stored, f.mimetype, "uploaded", now()))
    conn.commit()
    est = S.estimate(conn, tokens)
    conn.close()
    return jsonify(dict(est, id=sid, filename=original)), 201


# ---------------------------------------------------------------------------
# 2. Read: one paid call, SFU's outline, then the review draft
# ---------------------------------------------------------------------------
@bp.route("/api/syllabus/<sid>/read", methods=["POST"])
def read(sid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    row = load(conn, sid)
    path = os.path.join(UPLOAD_DIR, row["stored_name"])
    extract = current_app.config["EXTRACT_TEXT"]
    sem = active_semester(conn)
    term = {"name": sem["name"], "startDate": sem["start_date"], "endDate": sem["end_date"]}
    try:
        text = extract(path, row["filename"]) if S.ext_of(row["filename"]) not in ("pdf",) + tuple(S.IMAGE_TYPES) else None
        block, tokens = S.document_block(path, row["filename"], text)
        # the file name often carries the course code and term the document itself leaves out
        note = f'The uploaded file is named "{row["filename"]}".'
        if term.get("name"):
            note += f" The student's current term is {term['name']}."
        elif term.get("startDate"):
            note += f" The student's current term starts {term['startDate']}."
        raw, meta = S.read_syllabus(conn, block, tokens, note, bool(data.get("confirmed")))
    except ai.AiRefused as e:
        conn.close()
        return jsonify(e.payload), e.status

    c = raw.get("course") or {}
    hint = S.hints_from_filename(row["filename"])
    official, official_note = S.official_outline(c.get("code") or hint["code"], c.get("section"),
                                                 c.get("term") or hint["term"] or term.get("name"))
    draft = S.build_draft(raw, official, official_note, term, hint=row["filename"])
    class_id = data.get("classId") or row["class_id"]
    if class_id:
        draft["diff"] = compare(conn, class_id, draft)
    draft["raw"] = raw
    conn.execute("UPDATE syllabus_imports SET class_id=?, draft=?, official=?, model=?, input_tokens=?, output_tokens=?, "
                 "status='review' WHERE id=?",
                 (class_id, json.dumps(draft, default=str), json.dumps(official, default=str) if official else None,
                  meta["model"], meta["inputTokens"], meta["outputTokens"], sid))
    conn.commit()
    row = load(conn, sid)
    out = summary(row, json.loads(row["draft"]))
    out["usage"] = ai.spent_today(conn, ai.settings(conn))
    conn.close()
    return jsonify(out)


@bp.route("/api/syllabus/<sid>", methods=["GET", "DELETE"])
def one(sid):
    conn = get_db()
    row = load(conn, sid)
    if request.method == "DELETE":
        if row["status"] != "imported" and row["stored_name"]:
            try:
                os.remove(os.path.join(UPLOAD_DIR, row["stored_name"]))
            except OSError:
                pass
        conn.execute("DELETE FROM syllabus_imports WHERE id=?", (sid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    out = summary(row)
    conn.close()
    return jsonify(out)


# ---------------------------------------------------------------------------
# Re-import: what would change in a class that already exists
# ---------------------------------------------------------------------------
def compare(conn, class_id, draft):
    cls = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    if not cls:
        return None
    existing = conn.execute("SELECT * FROM items WHERE class_id=?", (class_id,)).fetchall()
    cats = {r["id"]: r for r in conn.execute("SELECT * FROM grade_categories WHERE class_id=?", (class_id,))}
    draft_cats = {g["id"]: g for g in draft["categories"]}
    used = set()

    def norm(t):
        return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()

    for it in draft["items"]:
        key = import_key(it["title"], it["type"])
        match = next((e for e in existing if e["id"] not in used and e["import_key"] == key), None) or \
            next((e for e in existing if e["id"] not in used and norm(e["title"]) == norm(it["title"])), None)
        if not match:
            it["change"] = "new"
            continue
        used.add(match["id"])
        it["existingId"] = match["id"]
        changes = []
        for field, before, after in (
                ("dueDate", match["due_date"], it["dueDate"]), ("dueTime", match["due_time"], it["dueTime"]),
                ("type", match["type"], it["type"]), ("location", match["location"] or "", it["location"] or "")):
            if (before or None) != (after or None):
                changes.append({"field": field, "before": before, "after": after})
        before_cat = cats.get(match["category_id"])
        after_cat = draft_cats.get(it["categoryId"])
        if (before_cat["name"].lower() if before_cat else None) != (after_cat["name"].lower() if after_cat else None):
            changes.append({"field": "category", "before": before_cat["name"] if before_cat else None,
                            "after": after_cat["name"] if after_cat else None})
        if not after_cat and match["weight"] != it["weight"]:
            changes.append({"field": "weight", "before": match["weight"], "after": it["weight"]})
        it["change"] = "changed" if changes else "same"
        it["changes"] = changes
        it["include"] = bool(changes)           # nothing to do for an unchanged item

    removed = [{"existingId": e["id"], "title": e["title"], "dueDate": e["due_date"], "include": False}
               for e in existing if e["id"] not in used and e["import_key"]]

    old_meet = sorted((m["kind"] or "lecture", m["day"], m["start"], m["end"], m["location"] or "")
                      for m in conn.execute("SELECT * FROM schedule_entries WHERE class_id=?", (class_id,)))
    new_meet = sorted((m["kind"], m["day"], m["start"], m["end"], m["location"] or "") for m in draft["meetings"])
    course = []
    for field, col in (("code", "code"), ("name", "name"), ("professor", "professor"), ("website", "website")):
        before, after = (cls[col] or "").strip(), (draft["course"].get(field) or "").strip()
        if after and before != after:
            course.append({"field": field, "before": before, "after": after, "include": not before})
    return {"classId": class_id, "removed": removed, "course": course,
            "meetingsChanged": old_meet != new_meet, "applyMeetings": old_meet != new_meet and not old_meet,
            "existingScale": bool(cls["grade_scale"])}


# ---------------------------------------------------------------------------
# 3. Import: write exactly what was reviewed, in one transaction
# ---------------------------------------------------------------------------
@bp.route("/api/syllabus/<sid>/import", methods=["POST"])
def do_import(sid):
    body = request.get_json(force=True) or {}
    draft = body.get("draft") or {}
    conn = get_db()
    row = load(conn, sid)
    if row["status"] == "imported":
        conn.close()
        return jsonify({"error": "This syllabus has already been imported."}), 409

    pending = [i["title"] for i in draft.get("items", []) if i.get("include") and not i.get("sure") and not i.get("confirmed")]
    pending += [f"{m['kind']} meeting" for m in draft.get("meetings", [])
                if m.get("include") and not m.get("sure") and not m.get("confirmed")]
    if pending:
        conn.close()
        n = len(pending)
        return jsonify({"error": f"{n} thing{'s' if n != 1 else ''} still {'need' if n != 1 else 'needs'} a look.",
                        "needsLook": pending}), 400

    c = draft.get("course") or {}
    if not (c.get("code") or c.get("name")):
        conn.close()
        return jsonify({"error": "Give the course a code or a name."}), 400

    diff = draft.get("diff") or {}
    class_id = diff.get("classId") or body.get("classId")
    t = now()
    counts = {"items": 0, "updated": 0, "removed": 0, "kept": 0, "meetings": 0, "categories": 0, "topics": 0}
    try:
        conn.execute("BEGIN")
        if class_id:
            if not conn.execute("SELECT 1 FROM classes WHERE id=?", (class_id,)).fetchone():
                raise ValueError("That class no longer exists.")
            for ch in diff.get("course") or []:
                if ch.get("include") and ch["field"] in ("code", "name", "professor", "website"):
                    conn.execute(f"UPDATE classes SET {ch['field']}=? WHERE id=?", (ch["after"], class_id))
        else:
            class_id = str(uuid.uuid4())
            conn.execute("INSERT INTO classes (id, semester_id, code, name, professor, color, notes, grade_scale, website, created_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (class_id, active_semester_id(conn), c.get("code") or "", c.get("name") or "", c.get("professor") or "",
                          body.get("color") or "", ("Instructor email: " + c["professorEmail"]) if c.get("professorEmail") else "",
                          None, c.get("website") or "", t))

        if draft.get("applyScale", True) and draft.get("gradeScale"):
            scale = [{"letter": s["letter"], "min": s["min"]} for s in draft["gradeScale"] if s.get("letter")]
            if scale:
                conn.execute("UPDATE classes SET grade_scale=? WHERE id=?", (json.dumps(scale), class_id))

        meetings = [m for m in draft.get("meetings", []) if m.get("include")]
        # a new class takes the reviewed meetings; an existing one only when the student chose to
        if not diff or diff.get("applyMeetings"):
            if diff:
                conn.execute("DELETE FROM schedule_entries WHERE class_id=?", (class_id,))
            for m in meetings:
                conn.execute("INSERT INTO schedule_entries (id, class_id, day, start, \"end\", location, kind, section, start_date, end_date)"
                             " VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (str(uuid.uuid4()), class_id, m["day"], m.get("start"), m.get("end"), m.get("location") or "",
                              m.get("kind") or "lecture", m.get("section") or "", m.get("startDate"), m.get("endDate")))
                counts["meetings"] += 1

        cat_ids = {}
        existing_cats = {(r["name"] or "").lower(): r["id"]
                         for r in conn.execute("SELECT id, name FROM grade_categories WHERE class_id=?", (class_id,))}
        for i, g in enumerate(x for x in draft.get("categories", []) if x.get("include", True)):
            gid = existing_cats.get((g.get("name") or "").lower())
            if gid:
                conn.execute("UPDATE grade_categories SET weight=?, drop_lowest=? WHERE id=?",
                             (g.get("weight"), int(g.get("dropLowest") or 0), gid))
            else:
                gid = str(uuid.uuid4())
                conn.execute("INSERT INTO grade_categories (id, class_id, name, weight, drop_lowest, sort_order, created_at)"
                             " VALUES (?,?,?,?,?,?,?)", (gid, class_id, g.get("name"), g.get("weight"),
                                                         int(g.get("dropLowest") or 0), i, t))
                counts["categories"] += 1
            cat_ids[g["id"]] = gid

        for it in draft.get("items", []):
            if not it.get("include"):
                continue
            cat = cat_ids.get(it.get("categoryId"))
            weight = None if cat else it.get("weight")
            if it.get("existingId"):
                # only schedule and weighting; never status, score or subtasks
                conn.execute("UPDATE items SET title=?, type=?, due_date=?, due_time=?, location=?, category_id=?, weight=?,"
                             " import_key=? WHERE id=? AND class_id=?",
                             (it["title"], it["type"], it.get("dueDate"), it.get("dueTime"), it.get("location") or "",
                              cat, weight, import_key(it["title"], it["type"]), it["existingId"], class_id))
                counts["updated"] += 1
            else:
                conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, due_date, due_time, status, weight, notes, created_at,"
                             " category_id, import_key, location) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (str(uuid.uuid4()), semester_for(conn, class_id), class_id, it["title"], it["type"], it.get("dueDate"), it.get("dueTime"),
                              "todo", weight, "", t, cat, import_key(it["title"], it["type"]), it.get("location") or ""))
                counts["items"] += 1

        for gone in diff.get("removed") or []:
            if not gone.get("include"):
                continue
            e = conn.execute("SELECT status, score FROM items WHERE id=? AND class_id=?", (gone["existingId"], class_id)).fetchone()
            if e and e["status"] != "done" and e["score"] is None:
                conn.execute("DELETE FROM items WHERE id=?", (gone["existingId"],))
                counts["removed"] += 1
            elif e:
                counts["kept"] += 1        # has a score or is done: that work is the student's

        have_topics = {(r["title"] or "").lower() for r in conn.execute("SELECT title FROM syllabus_topics WHERE class_id=?", (class_id,))}
        n = len(have_topics)
        for tp in draft.get("topics", []):
            if tp.get("include") and tp.get("title") and tp["title"].lower() not in have_topics:
                conn.execute("INSERT INTO syllabus_topics (id, class_id, title, done, sort_order) VALUES (?,?,?,0,?)",
                             (str(uuid.uuid4()), class_id, tp["title"], n))
                n += 1
                counts["topics"] += 1

        conn.execute("UPDATE semesters SET name=COALESCE(NULLIF(name,''),?), start_date=COALESCE(NULLIF(start_date,''),?),"
                     " end_date=COALESCE(NULLIF(end_date,''),?) WHERE id=?",
                     (c.get("term") or "", draft.get("firstDay") or "", draft.get("lastDay") or "",
                      active_semester_id(conn)))

        # The syllabus itself lands in the class's files, as its own copy of the
        # bytes. It used to point at the same stored file as the syllabus_imports
        # row, which meant two owners for one file: deleting either side silently
        # broke the other, and that is exactly what orphaned the first two syllabus
        # rows. A duplicated PDF costs a few hundred kilobytes; a file that vanishes
        # from the Files page because an import was tidied up costs a lot more.
        mid = str(uuid.uuid4())
        src = os.path.join(UPLOAD_DIR, row["stored_name"])
        extract = current_app.config["EXTRACT_TEXT"]
        stored, size, text = None, None, None
        if os.path.exists(src):
            stored = f"{mid}_{secure_filename(row['filename'] or 'syllabus')}"
            shutil.copyfile(src, os.path.join(UPLOAD_DIR, stored))
            size = os.path.getsize(os.path.join(UPLOAD_DIR, stored))
            text = extract(src, row["filename"])
        folder = folder_id_for_kind(conn, class_id, "syllabus") if class_id else None
        conn.execute("INSERT INTO materials (id, semester_id, class_id, category, folder_id, title, kind, filename, stored_name, mimetype, size,"
                     " extracted_text, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (mid, semester_for(conn, class_id), class_id, "syllabus", folder,
                      "Syllabus" if not diff else f"Syllabus (updated {t[:10]})", "file", row["filename"],
                      stored, row["mimetype"], size, text, t))

        conn.execute("UPDATE syllabus_imports SET status='imported', class_id=?, imported_at=?, draft=? WHERE id=?",
                     (class_id, t, json.dumps(draft, default=str), sid))
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        conn.close()
        return jsonify({"error": f"Nothing was imported: {e}"}), 400
    conn.close()
    return jsonify({"classId": class_id, "counts": counts}), 201

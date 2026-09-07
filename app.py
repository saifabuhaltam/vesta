import os
import uuid
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, send_from_directory, abort, Response
from werkzeug.utils import secure_filename

from db import get_db, init_db, UPLOAD_DIR

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB per upload

init_db()


# ---------------- serializers ----------------

def serialize_material(m):
    d = {
        "id": m["id"],
        "category": m["category"],
        "title": m["title"],
        "kind": m["kind"],
        "createdAt": m["created_at"],
    }
    if m["kind"] == "file":
        d["filename"] = m["filename"]
        d["size"] = m["size"]
        d["mimetype"] = m["mimetype"]
        d["url"] = f"/api/materials/{m['id']}/download"
    else:
        d["url"] = m["url"]
    return d


def serialize_note(n):
    return {
        "id": n["id"],
        "text": n["text"],
        "linkedItemId": n["linked_item_id"],
        "createdAt": n["created_at"],
    }


def serialize_class(conn, row):
    schedule = conn.execute(
        "SELECT day, start, end, location FROM schedule_entries WHERE class_id=?",
        (row["id"],),
    ).fetchall()
    materials = conn.execute(
        "SELECT * FROM materials WHERE class_id=? ORDER BY created_at", (row["id"],)
    ).fetchall()
    notes = conn.execute(
        "SELECT * FROM notes WHERE class_id=? ORDER BY created_at", (row["id"],)
    ).fetchall()
    topics = conn.execute(
        "SELECT * FROM syllabus_topics WHERE class_id=? ORDER BY sort_order, rowid",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "professor": row["professor"],
        "color": row["color"],
        "notes": row["notes"],
        "createdAt": row["created_at"],
        "schedule": [
            {"day": s["day"], "start": s["start"], "end": s["end"], "location": s["location"]}
            for s in schedule
        ],
        "materials": [serialize_material(m) for m in materials],
        "notesList": [serialize_note(n) for n in notes],
        "syllabus": [
            {"id": t["id"], "title": t["title"], "done": bool(t["done"])} for t in topics
        ],
    }


def serialize_item(conn, row):
    subtasks = conn.execute(
        "SELECT * FROM subtasks WHERE item_id=?", (row["id"],)
    ).fetchall()
    return {
        "id": row["id"],
        "classId": row["class_id"],
        "title": row["title"],
        "type": row["type"],
        "dueDate": row["due_date"],
        "dueTime": row["due_time"],
        "status": row["status"],
        "completedAt": row["completed_at"],
        "weight": row["weight"],
        "score": row["score"],
        "notes": row["notes"],
        "createdAt": row["created_at"],
        "subtasks": [
            {"id": s["id"], "title": s["title"], "done": bool(s["done"])} for s in subtasks
        ],
    }


def extract_text(filepath, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if ext == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(filepath)
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            return text[:200000]
        if ext == "docx":
            import docx

            d = docx.Document(filepath)
            text = "\n".join(p.text for p in d.paragraphs)
            return text[:200000]
    except Exception:
        return None
    return None


# ---------------- frontend ----------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------- state ----------------

@app.route("/api/state")
def get_state():
    conn = get_db()
    classes = conn.execute("SELECT * FROM classes ORDER BY created_at").fetchall()
    items = conn.execute("SELECT * FROM items ORDER BY created_at").fetchall()
    term = conn.execute("SELECT * FROM term_settings WHERE id=1").fetchone()
    result = {
        "classes": [serialize_class(conn, c) for c in classes],
        "items": [serialize_item(conn, i) for i in items],
        "term": {
            "name": term["name"] or "",
            "startDate": term["start_date"] or "",
            "endDate": term["end_date"] or "",
        },
    }
    conn.close()
    return jsonify(result)


@app.route("/api/term", methods=["PUT"])
def update_term():
    data = request.get_json(force=True) or {}
    conn = get_db()
    conn.execute(
        "UPDATE term_settings SET name=?, start_date=?, end_date=? WHERE id=1",
        (data.get("name", ""), data.get("startDate", ""), data.get("endDate", "")),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------- classes ----------------

@app.route("/api/classes", methods=["POST"])
def create_class():
    data = request.get_json(force=True) or {}
    conn = get_db()
    cid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO classes (id, code, name, professor, color, notes, created_at) VALUES (?,?,?,?,?,?,?)",
        (
            cid,
            data.get("code", ""),
            data.get("name", ""),
            data.get("professor", ""),
            data.get("color", ""),
            data.get("notes", ""),
            now,
        ),
    )
    for s in data.get("schedule", []) or []:
        conn.execute(
            "INSERT INTO schedule_entries (id, class_id, day, start, end, location) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), cid, s.get("day"), s.get("start"), s.get("end"), s.get("location", "")),
        )
    conn.commit()
    conn.close()
    return jsonify({"id": cid}), 201


@app.route("/api/classes/<cid>", methods=["PUT"])
def update_class(cid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    colmap = {"code": "code", "name": "name", "professor": "professor", "color": "color", "notes": "notes"}
    fields, values = [], []
    for key, col in colmap.items():
        if key in data:
            fields.append(f"{col}=?")
            values.append(data[key])
    if fields:
        values.append(cid)
        conn.execute(f"UPDATE classes SET {', '.join(fields)} WHERE id=?", values)
    if "schedule" in data:
        conn.execute("DELETE FROM schedule_entries WHERE class_id=?", (cid,))
        for s in data["schedule"] or []:
            conn.execute(
                "INSERT INTO schedule_entries (id, class_id, day, start, end, location) VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), cid, s.get("day"), s.get("start"), s.get("end"), s.get("location", "")),
            )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/classes/<cid>", methods=["DELETE"])
def delete_class(cid):
    conn = get_db()
    mats = conn.execute(
        "SELECT stored_name FROM materials WHERE class_id=? AND kind='file'", (cid,)
    ).fetchall()
    conn.execute("DELETE FROM classes WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    for m in mats:
        if m["stored_name"]:
            path = os.path.join(UPLOAD_DIR, m["stored_name"])
            if os.path.exists(path):
                os.remove(path)
    return jsonify({"ok": True})


# ---------------- items ----------------

@app.route("/api/items", methods=["POST"])
def create_item():
    data = request.get_json(force=True) or {}
    conn = get_db()
    iid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT INTO items (id, class_id, title, type, due_date, due_time, status, completed_at, weight, score, notes, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            iid,
            data.get("classId"),
            data.get("title", ""),
            data.get("type", "assignment"),
            data.get("dueDate"),
            data.get("dueTime"),
            data.get("status", "todo"),
            data.get("completedAt"),
            data.get("weight"),
            data.get("score"),
            data.get("notes", ""),
            now,
        ),
    )
    for s in data.get("subtasks", []) or []:
        conn.execute(
            "INSERT INTO subtasks (id, item_id, title, done) VALUES (?,?,?,?)",
            (s.get("id") or str(uuid.uuid4()), iid, s.get("title", ""), 1 if s.get("done") else 0),
        )
    conn.commit()
    conn.close()
    return jsonify({"id": iid}), 201


@app.route("/api/items/<iid>", methods=["PUT"])
def update_item(iid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    colmap = {
        "classId": "class_id",
        "title": "title",
        "type": "type",
        "dueDate": "due_date",
        "dueTime": "due_time",
        "status": "status",
        "completedAt": "completed_at",
        "weight": "weight",
        "score": "score",
        "notes": "notes",
    }
    fields, values = [], []
    for key, col in colmap.items():
        if key in data:
            fields.append(f"{col}=?")
            values.append(data[key])
    if fields:
        values.append(iid)
        conn.execute(f"UPDATE items SET {', '.join(fields)} WHERE id=?", values)
    if "subtasks" in data:
        conn.execute("DELETE FROM subtasks WHERE item_id=?", (iid,))
        for s in data["subtasks"] or []:
            conn.execute(
                "INSERT INTO subtasks (id, item_id, title, done) VALUES (?,?,?,?)",
                (s.get("id") or str(uuid.uuid4()), iid, s.get("title", ""), 1 if s.get("done") else 0),
            )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/items/<iid>", methods=["DELETE"])
def delete_item(iid):
    conn = get_db()
    conn.execute("DELETE FROM items WHERE id=?", (iid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------- materials (files & links) ----------------

@app.route("/api/classes/<cid>/materials", methods=["POST"])
def add_material(cid):
    conn = get_db()
    cls = conn.execute("SELECT id FROM classes WHERE id=?", (cid,)).fetchone()
    if not cls:
        conn.close()
        abort(404)
    now = datetime.utcnow().isoformat()
    mid = str(uuid.uuid4())

    if "file" in request.files and request.files["file"].filename:
        f = request.files["file"]
        original = secure_filename(f.filename) or "file"
        stored = f"{mid}_{original}"
        path = os.path.join(UPLOAD_DIR, stored)
        f.save(path)
        size = os.path.getsize(path)
        mimetype = f.mimetype
        category = request.form.get("category", "other")
        title = request.form.get("title") or original
        text = extract_text(path, original)
        conn.execute(
            """INSERT INTO materials (id, class_id, category, title, kind, url, filename, stored_name, mimetype, size, extracted_text, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, cid, category, title, "file", None, original, stored, mimetype, size, text, now),
        )
    else:
        data = request.get_json(silent=True) or request.form
        url = (data.get("url") or "").strip()
        if not url:
            conn.close()
            return jsonify({"error": "url or file required"}), 400
        title = data.get("title") or url
        category = data.get("category", "other")
        conn.execute(
            """INSERT INTO materials (id, class_id, category, title, kind, url, filename, stored_name, mimetype, size, extracted_text, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, cid, category, title, "link", url, None, None, None, None, None, now),
        )
    conn.commit()
    conn.close()
    return jsonify({"id": mid}), 201


@app.route("/api/materials/<mid>", methods=["DELETE"])
def delete_material(mid):
    conn = get_db()
    m = conn.execute("SELECT stored_name, kind FROM materials WHERE id=?", (mid,)).fetchone()
    conn.execute("DELETE FROM materials WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    if m and m["kind"] == "file" and m["stored_name"]:
        path = os.path.join(UPLOAD_DIR, m["stored_name"])
        if os.path.exists(path):
            os.remove(path)
    return jsonify({"ok": True})


@app.route("/api/materials/<mid>/download")
def download_material(mid):
    conn = get_db()
    m = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    conn.close()
    if not m or m["kind"] != "file":
        abort(404)
    return send_from_directory(UPLOAD_DIR, m["stored_name"], as_attachment=True, download_name=m["filename"])


# ---------------- notes ----------------

@app.route("/api/classes/<cid>/notes", methods=["POST"])
def add_note(cid):
    data = request.get_json(force=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    conn = get_db()
    rows = conn.execute("SELECT id, title FROM items WHERE class_id=?", (cid,)).fetchall()
    lower = text.lower()
    matched = None
    for it in rows:
        title = it["title"] or ""
        if title and title.lower() in lower:
            if not matched or len(title) > len(matched["title"]):
                matched = it
    nid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO notes (id, class_id, text, linked_item_id, created_at) VALUES (?,?,?,?,?)",
        (nid, cid, text, matched["id"] if matched else None, now),
    )
    conn.commit()
    conn.close()
    return jsonify({"id": nid}), 201


@app.route("/api/notes/<nid>", methods=["DELETE"])
def delete_note(nid):
    conn = get_db()
    conn.execute("DELETE FROM notes WHERE id=?", (nid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------- syllabus ----------------

@app.route("/api/classes/<cid>/syllabus", methods=["POST"])
def add_topic(cid):
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    conn = get_db()
    tid = str(uuid.uuid4())
    count = conn.execute(
        "SELECT COUNT(*) c FROM syllabus_topics WHERE class_id=?", (cid,)
    ).fetchone()["c"]
    conn.execute(
        "INSERT INTO syllabus_topics (id, class_id, title, done, sort_order) VALUES (?,?,?,0,?)",
        (tid, cid, title, count),
    )
    conn.commit()
    conn.close()
    return jsonify({"id": tid}), 201


@app.route("/api/syllabus/<tid>", methods=["PUT"])
def update_topic(tid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    conn.execute("UPDATE syllabus_topics SET done=? WHERE id=?", (1 if data.get("done") else 0, tid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/syllabus/<tid>", methods=["DELETE"])
def delete_topic(tid):
    conn = get_db()
    conn.execute("DELETE FROM syllabus_topics WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------- calendar export ----------------

@app.route("/api/export.ics")
def export_ics():
    conn = get_db()
    classes = conn.execute("SELECT * FROM classes").fetchall()
    items_rows = conn.execute(
        "SELECT * FROM items WHERE due_date IS NOT NULL AND due_date != ''"
    ).fetchall()
    term = conn.execute("SELECT * FROM term_settings WHERE id=1").fetchone()
    class_map = {c["id"]: c for c in classes}

    def esc(s):
        return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Term Board//EN", "CALSCALE:GREGORIAN"]
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    if term["start_date"] and term["end_date"]:
        start = datetime.strptime(term["start_date"], "%Y-%m-%d")
        end = datetime.strptime(term["end_date"], "%Y-%m-%d")
        until_str = end.strftime("%Y%m%dT235959Z")
        for c in classes:
            schedule = conn.execute(
                "SELECT * FROM schedule_entries WHERE class_id=?", (c["id"],)
            ).fetchall()
            for s in schedule:
                if not s["start"] or not s["end"]:
                    continue
                first = start
                guard = 0
                target_weekday = (s["day"] - 1) % 7  # JS day 0=Sun -> Python weekday 0=Mon
                while first.weekday() != target_weekday and guard < 7:
                    first += timedelta(days=1)
                    guard += 1
                sh, sm = map(int, s["start"].split(":"))
                eh, em = map(int, s["end"].split(":"))
                dtstart = first.replace(hour=sh, minute=sm).strftime("%Y%m%dT%H%M%S")
                dtend = first.replace(hour=eh, minute=em).strftime("%Y%m%dT%H%M%S")
                lines += [
                    "BEGIN:VEVENT",
                    f"UID:{c['id']}-{s['day']}-{s['start'].replace(':','')}@term-board",
                    f"DTSTAMP:{stamp}",
                    f"DTSTART:{dtstart}",
                    f"DTEND:{dtend}",
                    f"RRULE:FREQ=WEEKLY;UNTIL={until_str}",
                    f"SUMMARY:{esc((c['code'] or '') + ' — ' + (c['name'] or ''))}",
                ]
                if s["location"]:
                    lines.append(f"LOCATION:{esc(s['location'])}")
                lines.append("END:VEVENT")

    for it in items_rows:
        cls = class_map.get(it["class_id"])
        summary = (f"{cls['code']}: " if cls else "") + (it["title"] or "")
        lines += ["BEGIN:VEVENT", f"UID:{it['id']}@term-board", f"DTSTAMP:{stamp}"]
        if it["due_time"]:
            h, m = map(int, it["due_time"].split(":"))
            d = datetime.strptime(it["due_date"], "%Y-%m-%d").replace(hour=h, minute=m)
            dend = d + timedelta(minutes=30)
            lines.append(f"DTSTART:{d.strftime('%Y%m%dT%H%M%S')}")
            lines.append(f"DTEND:{dend.strftime('%Y%m%dT%H%M%S')}")
        else:
            compact = it["due_date"].replace("-", "")
            lines.append(f"DTSTART;VALUE=DATE:{compact}")
            lines.append(f"DTEND;VALUE=DATE:{compact}")
        lines.append(f"SUMMARY:{esc(summary)}")
        if it["notes"]:
            lines.append(f"DESCRIPTION:{esc(it['notes'])}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    conn.close()
    ics_text = "\r\n".join(lines)
    return Response(
        ics_text,
        mimetype="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="term-board-calendar.ics"'},
    )


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "File is larger than 25 MB."}), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")

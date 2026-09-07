import os
import json
import uuid
from datetime import datetime, timedelta

import anthropic
from flask import Flask, request, jsonify, send_from_directory, abort, Response
from werkzeug.utils import secure_filename

from db import get_db, init_db, UPLOAD_DIR

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB per upload

init_db()

# ---------------- Headstart ----------------

HEADSTART_LABELS = {
    "draft": "Generate draft",
    "essay_outline": "Generate essay outline",
    "quiz_prep": "Generate quiz prep",
    "study_outline": "Generate study outline",
    "synthesis": "Synthesize relevant readings",
    "explain": "Explain assignment requirements",
}

HEADSTART_KIND_BY_TYPE = {
    "assignment": ["draft", "essay_outline", "explain"],
    "project": ["draft", "essay_outline", "explain"],
    "quiz": ["quiz_prep", "explain"],
    "exam": ["study_outline", "explain"],
    "reading": ["synthesis", "explain"],
    "other": list(HEADSTART_LABELS.keys()),
}

HEADSTART_KIND_INSTRUCTIONS = {
    "draft": (
        "Write a first draft for this assignment based on the description below. "
        "This is a starting point for the student to revise and build on, not a finished "
        "submission - write in a natural voice appropriate for a college student, and note in "
        "a short closing line any places where the student should add their own specifics "
        "(data, personal examples, citations) that aren't given here."
    ),
    "essay_outline": (
        "Create a structured essay outline for this assignment: a working thesis, main "
        "sections with a one-line description of what each covers, and key points or evidence "
        "to include under each section."
    ),
    "quiz_prep": (
        "Create a quiz preparation guide: the topics most likely to be tested, key terms with "
        "brief definitions, and 5-8 practice questions with answers, based on the context below."
    ),
    "study_outline": (
        "Create a study outline for this exam: the major topics to review in a logical study "
        "order, with sub-points under each, and a short note on what's likely to be emphasized "
        "based on the context given."
    ),
    "synthesis": (
        "Synthesize the key ideas across the readings provided below as they relate to this "
        "assignment: the main themes, where the sources agree or disagree, and how they connect "
        "to what the assignment is asking for."
    ),
    "explain": (
        "Explain in plain terms what this assignment is actually asking for: the deliverable, "
        "the format if evident, likely grading criteria, and what a strong submission would "
        "include."
    ),
}


def build_headstart_prompt(item, cls, kind, materials_text, revise_content=None, instructions=None, rubric_text=None):
    context = ["Assignment: " + (item["title"] or ""), "Type: " + (item["type"] or "")]
    if cls:
        context.append("Class: " + (cls["code"] or "") + " - " + (cls["name"] or ""))
    if item["due_date"]:
        context.append("Due: " + item["due_date"])
    if item["notes"]:
        context.append("Description/notes from the student: " + item["notes"])
    if rubric_text:
        context.append("Grading rubric:\n" + rubric_text)
    if kind == "synthesis" and materials_text:
        context.append("Relevant reading material:\n" + materials_text)

    context_block = "\n".join(context)
    instruction = HEADSTART_KIND_INSTRUCTIONS[kind]

    if revise_content and instructions:
        return (
            f"{context_block}\n\nTask: {instruction}\n\n"
            f"Here is the current version:\n---\n{revise_content}\n---\n\n"
            f'The student asked for this revision: "{instructions}"\n\n'
            "Produce the full revised version incorporating that feedback. "
            "Return only the revised content, no preamble."
        )
    return (
        f"{context_block}\n\nTask: {instruction}\n\n"
        "Return only the content itself, no preamble or meta-commentary about what you're about to do."
    )


def serialize_headstart(r):
    return {
        "id": r["id"],
        "itemId": r["item_id"],
        "kind": r["kind"],
        "content": r["content"],
        "status": r["status"],
        "instructions": json.loads(r["instructions"]) if r["instructions"] else [],
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


# ---------------- serializers ----------------

FILE_CATEGORY_RULES = [
    (r"syllabus", "syllabus"),
    (r"rubric", "rubrics"),
    (r"midterm|final[\s_-]?exam|past[\s_-]?exam|exam[\s_-]?\d|old[\s_-]?exam", "exams"),
    (r"lecture|slides|week[\s_-]?\d+|lesson[\s_-]?\d+", "slides"),
    (r"reading|article|chapter|excerpt", "readings"),
    (r"project", "projects"),
    (r"notes|journal", "personal"),
]


def guess_file_category(filename):
    import re

    name = (filename or "").lower()
    for pattern, category in FILE_CATEGORY_RULES:
        if re.search(pattern, name):
            return category
    return "other"


def serialize_material(m):
    d = {
        "id": m["id"],
        "category": m["category"],
        "title": m["title"],
        "kind": m["kind"],
        "hasText": bool(m["extracted_text"]),
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


def serialize_rubric(r):
    if not r:
        return None
    return {
        "id": r["id"],
        "materialId": r["material_id"],
        "itemId": r["item_id"],
        "totalPoints": r["total_points"],
        "criteria": json.loads(r["criteria"]) if r["criteria"] else [],
        "createdAt": r["created_at"],
    }


def serialize_item(conn, row):
    subtasks = conn.execute(
        "SELECT * FROM subtasks WHERE item_id=?", (row["id"],)
    ).fetchall()
    headstarts = conn.execute(
        "SELECT kind, status FROM headstarts WHERE item_id=?", (row["id"],)
    ).fetchall()
    rubric_row = conn.execute(
        "SELECT * FROM rubrics WHERE item_id=?", (row["id"],)
    ).fetchone()
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
        "focusSeconds": row["focus_seconds"] or 0,
        "createdAt": row["created_at"],
        "subtasks": [
            {"id": s["id"], "title": s["title"], "done": bool(s["done"])} for s in subtasks
        ],
        "headstarts": [{"kind": h["kind"], "status": h["status"]} for h in headstarts],
        "rubric": serialize_rubric(rubric_row),
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


# ---------------- headstart ----------------

@app.route("/api/items/<iid>/headstarts", methods=["GET"])
def list_headstarts(iid):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM headstarts WHERE item_id=? ORDER BY created_at", (iid,)
    ).fetchall()
    conn.close()
    return jsonify([serialize_headstart(r) for r in rows])


@app.route("/api/items/<iid>/headstart", methods=["POST"])
def generate_headstart(iid):
    data = request.get_json(force=True) or {}
    kind = data.get("kind")
    mode = data.get("mode", "generate")
    instructions = (data.get("instructions") or "").strip()
    if kind not in HEADSTART_LABELS:
        return jsonify({"error": "Unknown headstart kind"}), 400

    conn = get_db()
    item = conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not item:
        conn.close()
        abort(404)
    cls = (
        conn.execute("SELECT * FROM classes WHERE id=?", (item["class_id"],)).fetchone()
        if item["class_id"]
        else None
    )

    materials_text = None
    if kind == "synthesis" and cls:
        mats = conn.execute(
            "SELECT title, extracted_text FROM materials WHERE class_id=? AND extracted_text IS NOT NULL AND extracted_text != '' ORDER BY created_at LIMIT 4",
            (cls["id"],),
        ).fetchall()
        if mats:
            materials_text = "\n\n".join(
                f"[{m['title']}]\n{(m['extracted_text'] or '')[:4000]}" for m in mats
            )

    existing = conn.execute(
        "SELECT * FROM headstarts WHERE item_id=? AND kind=?", (iid, kind)
    ).fetchone()

    rubric_row = conn.execute("SELECT * FROM rubrics WHERE item_id=?", (iid,)).fetchone()
    rubric_text = None
    if rubric_row and rubric_row["criteria"]:
        crit_list = json.loads(rubric_row["criteria"])
        if crit_list:
            lines = []
            for c in crit_list:
                pts = c.get("points")
                line = "- " + (c.get("name") or "")
                if pts is not None:
                    line += f" ({pts} pts)"
                if c.get("description"):
                    line += ": " + c["description"]
                lines.append(line)
            rubric_text = "\n".join(lines)

    revise_content = None
    hist = []
    if mode == "revise":
        if not existing:
            conn.close()
            return jsonify({"error": "Nothing to revise yet - generate first."}), 400
        if not instructions:
            conn.close()
            return jsonify({"error": "Revision instructions required"}), 400
        revise_content = existing["content"]
        hist = json.loads(existing["instructions"]) if existing["instructions"] else []

    prompt = build_headstart_prompt(
        item,
        cls,
        kind,
        materials_text,
        revise_content,
        instructions if mode == "revise" else None,
        rubric_text,
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
        content = "\n".join(b.text for b in response.content if b.type == "text").strip()
        if not content:
            conn.close()
            return jsonify({"error": "Claude didn't return any text. Try again or adjust the assignment notes."}), 502
    except anthropic.AuthenticationError:
        conn.close()
        return jsonify({"error": "Invalid or missing ANTHROPIC_API_KEY. Set it in Railway's Variables tab."}), 503
    except anthropic.RateLimitError:
        conn.close()
        return jsonify({"error": "Rate limited by the Claude API. Wait a moment and try again."}), 429
    except anthropic.APIStatusError as e:
        conn.close()
        return jsonify({"error": f"Claude API error: {e.message}"}), 502
    except anthropic.APIConnectionError:
        conn.close()
        return jsonify({"error": "Could not reach the Claude API. Check the server's network connection."}), 502
    except Exception as e:
        conn.close()
        return jsonify({"error": f"ANTHROPIC_API_KEY may not be set. ({e})"}), 503

    now = datetime.utcnow().isoformat()
    if mode == "revise":
        hist.append({"instruction": instructions, "at": now})
        hid = existing["id"]
        conn.execute(
            "UPDATE headstarts SET content=?, status='draft', instructions=?, updated_at=? WHERE id=?",
            (content, json.dumps(hist), now, hid),
        )
    elif existing:
        hid = existing["id"]
        conn.execute(
            "UPDATE headstarts SET content=?, status='draft', instructions='[]', updated_at=? WHERE id=?",
            (content, now, hid),
        )
    else:
        hid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO headstarts (id, item_id, kind, content, status, instructions, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (hid, iid, kind, content, "draft", "[]", now, now),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM headstarts WHERE id=?", (hid,)).fetchone()
    conn.close()
    return jsonify(serialize_headstart(row)), 201


@app.route("/api/headstarts/<hid>", methods=["PUT"])
def update_headstart(hid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    fields, values = [], []
    if "content" in data:
        fields.append("content=?")
        values.append(data["content"])
    if "status" in data:
        fields.append("status=?")
        values.append(data["status"])
    if fields:
        fields.append("updated_at=?")
        values.append(datetime.utcnow().isoformat())
        values.append(hid)
        conn.execute(f"UPDATE headstarts SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    row = conn.execute("SELECT * FROM headstarts WHERE id=?", (hid,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    return jsonify(serialize_headstart(row))


@app.route("/api/headstarts/<hid>", methods=["DELETE"])
def delete_headstart(hid):
    conn = get_db()
    conn.execute("DELETE FROM headstarts WHERE id=?", (hid,))
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
        category = request.form.get("category") or guess_file_category(original)
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


@app.route("/api/materials/<mid>", methods=["PUT"])
def update_material(mid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    fields, values = [], []
    if "category" in data:
        fields.append("category=?")
        values.append(data["category"])
    if "title" in data:
        fields.append("title=?")
        values.append(data["title"])
    if fields:
        values.append(mid)
        conn.execute(f"UPDATE materials SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    row = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    return jsonify(serialize_material(row))


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


# ---------------- rubrics ----------------

RUBRIC_PARSE_INSTRUCTION = (
    "You are extracting a grading rubric from the raw text below. Return ONLY valid JSON, "
    "no markdown code fences, no commentary, in exactly this shape:\n"
    '{"criteria": [{"name": "...", "description": "...", "points": <number or null>}], '
    '"totalPoints": <number or null>}\n'
    "If a criterion is expressed as a percentage rather than raw points, put the percentage "
    "number in \"points\" and say so in \"description\". If you can't find real grading "
    "criteria in the text, return {\"criteria\": [], \"totalPoints\": null}."
)


@app.route("/api/materials/<mid>/rubric")
def get_rubric(mid):
    conn = get_db()
    row = conn.execute("SELECT * FROM rubrics WHERE material_id=?", (mid,)).fetchone()
    conn.close()
    return jsonify(serialize_rubric(row))


@app.route("/api/materials/<mid>/parse-rubric", methods=["POST"])
def parse_rubric(mid):
    conn = get_db()
    m = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    if not m:
        conn.close()
        abort(404)
    if not m["extracted_text"]:
        conn.close()
        return jsonify({"error": "No extractable text in this file - only PDF and DOCX get parsed on upload."}), 400

    prompt = RUBRIC_PARSE_INSTRUCTION + "\n\n---\n" + m["extracted_text"][:12000]

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
        parsed = json.loads(text)
    except anthropic.AuthenticationError:
        conn.close()
        return jsonify({"error": "Invalid or missing ANTHROPIC_API_KEY. Set it in Railway's Variables tab."}), 503
    except anthropic.RateLimitError:
        conn.close()
        return jsonify({"error": "Rate limited by the Claude API. Wait a moment and try again."}), 429
    except anthropic.APIStatusError as e:
        conn.close()
        return jsonify({"error": f"Claude API error: {e.message}"}), 502
    except anthropic.APIConnectionError:
        conn.close()
        return jsonify({"error": "Could not reach the Claude API."}), 502
    except (ValueError, json.JSONDecodeError):
        conn.close()
        return jsonify({"error": "Couldn't parse a rubric out of Claude's response. Try again."}), 502
    except Exception as e:
        conn.close()
        return jsonify({"error": f"ANTHROPIC_API_KEY may not be set. ({e})"}), 503

    criteria = parsed.get("criteria") or []
    total_points = parsed.get("totalPoints")
    now = datetime.utcnow().isoformat()
    existing = conn.execute("SELECT id FROM rubrics WHERE material_id=?", (mid,)).fetchone()
    if existing:
        rid = existing["id"]
        conn.execute(
            "UPDATE rubrics SET criteria=?, total_points=?, created_at=? WHERE id=?",
            (json.dumps(criteria), total_points, now, rid),
        )
    else:
        rid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO rubrics (id, material_id, item_id, criteria, total_points, created_at) VALUES (?,?,?,?,?,?)",
            (rid, mid, None, json.dumps(criteria), total_points, now),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM rubrics WHERE id=?", (rid,)).fetchone()
    conn.close()
    return jsonify(serialize_rubric(row)), 201


@app.route("/api/rubrics/<rid>", methods=["PUT"])
def update_rubric(rid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    fields, values = [], []
    if "itemId" in data:
        fields.append("item_id=?")
        values.append(data["itemId"] or None)
    if "criteria" in data:
        fields.append("criteria=?")
        values.append(json.dumps(data["criteria"]))
    if "totalPoints" in data:
        fields.append("total_points=?")
        values.append(data["totalPoints"])
    if fields:
        values.append(rid)
        conn.execute(f"UPDATE rubrics SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    row = conn.execute("SELECT * FROM rubrics WHERE id=?", (rid,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    return jsonify(serialize_rubric(row))


@app.route("/api/rubrics/<rid>", methods=["DELETE"])
def delete_rubric(rid):
    conn = get_db()
    conn.execute("DELETE FROM rubrics WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------- focus time (Lock In) ----------------

@app.route("/api/items/<iid>/focus-time", methods=["POST"])
def add_focus_time(iid):
    data = request.get_json(force=True) or {}
    seconds = int(data.get("seconds") or 0)
    conn = get_db()
    row = conn.execute("SELECT id FROM items WHERE id=?", (iid,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    if seconds > 0:
        conn.execute(
            "UPDATE items SET focus_seconds = COALESCE(focus_seconds,0) + ? WHERE id=?",
            (seconds, iid),
        )
        conn.commit()
    updated = conn.execute("SELECT focus_seconds FROM items WHERE id=?", (iid,)).fetchone()
    conn.close()
    return jsonify({"focusSeconds": updated["focus_seconds"] or 0})


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

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Vesta//EN", "CALSCALE:GREGORIAN"]
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
                    f"UID:{c['id']}-{s['day']}-{s['start'].replace(':','')}@vesta",
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
        lines += ["BEGIN:VEVENT", f"UID:{it['id']}@vesta", f"DTSTAMP:{stamp}"]
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
        headers={"Content-Disposition": 'attachment; filename="vesta-calendar.ics"'},
    )


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "File is larger than 25 MB."}), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")

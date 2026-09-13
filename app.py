import os
import re
import mimetypes
import json
import uuid
from datetime import datetime, timedelta

import anthropic
from flask import Flask, request, jsonify, send_from_directory, abort, Response
from werkzeug.utils import secure_filename

# .env is read before anything else is imported: db.py decides where the database
# and uploads live (DATA_DIR) the moment it is imported.
def load_env_file(path):
    """Read KEY=value lines from a local .env, so the API key never has to live in code.

    No dependency, no logging: values go straight into the environment and are
    never printed. Anything already set in the real environment wins.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip().strip('"').strip("'")
            # a blank placeholder line should not count as a key
            if key and value and key not in os.environ:
                os.environ[key] = value


load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


from db import get_db, init_db, UPLOAD_DIR



app = Flask(__name__, static_folder="static", static_url_path="")

# The AI layer lives in its own module: Headstart tools, Quiz Me, flashcards and
# practice tests, plus the cost guardrails they all share.
from ai import bp as ai_bp  # noqa: E402
app.register_blueprint(ai_bp)
from links import bp as links_bp  # noqa: E402
app.register_blueprint(links_bp)
# syllabus import reads uploads with the same extractor the rest of the app uses
# looked up at call time: extract_text is defined further down this file
app.config["EXTRACT_TEXT"] = lambda path, name: extract_text(path, name)
from syllabus_import import bp as syllabus_bp  # noqa: E402
app.register_blueprint(syllabus_bp)
# calendar: SFU's published timetable now, connected calendars next
from calendar_api import bp as calendar_bp  # noqa: E402
app.register_blueprint(calendar_bp)
# Accounts, and the gate in front of every /api route. Entirely a no-op locally,
# where no Supabase is configured: Vesta stays the single-user tool it started as.
import auth as vesta_auth  # noqa: E402
vesta_auth.install(app)
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
    "homework": ["draft", "explain"],
    "discussion": ["draft", "explain"],
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


def item_ids_for_material(conn, mid):
    """Every assignment a file is attached to. A file is stored once and can serve many."""
    return [r["item_id"] for r in conn.execute(
        "SELECT item_id FROM item_files WHERE material_id=? ORDER BY created_at", (mid,))]


def serialize_material(m, item_ids=None):
    item_ids = item_ids or []
    d = {
        "id": m["id"],
        "itemIds": item_ids,
        "itemId": item_ids[0] if item_ids else None,   # older callers read a single id
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
        # the row can outlive the upload (a deleted class removes the file), and
        # a preview that says so beats an empty frame
        d["missing"] = not (m["stored_name"] and os.path.exists(os.path.join(UPLOAD_DIR, m["stored_name"])))
    else:
        d["url"] = m["url"]
    return d


def serialize_note(n):
    keys = n.keys()
    return {
        "id": n["id"],
        "title": n["title"] or "",
        "folderId": n["folder_id"],
        "text": n["text"],
        "linkedItemId": n["linked_item_id"],
        "pinned": bool(n["pinned"]) if "pinned" in keys else False,
        "starred": bool(n["starred"]) if "starred" in keys else False,
        "sortOrder": (n["sort_order"] or 0) if "sort_order" in keys else 0,
        "deletedAt": n["deleted_at"] if "deleted_at" in keys else None,
        "updatedAt": n["updated_at"] or n["created_at"],
        "createdAt": n["created_at"],
    }


def folder_would_cycle(conn, fid, parent_id):
    """Walk up from the proposed parent; if we meet ourselves it is a loop."""
    cursor, hops = parent_id, 0
    while cursor:
        if cursor == fid:
            return True
        hops += 1
        if hops > 50:
            return True
        row = conn.execute("SELECT parent_id FROM note_folders WHERE id=?", (cursor,)).fetchone()
        cursor = row["parent_id"] if row else None
    return False


def serialize_event(e):
    return {
        "id": e["id"],
        "classId": e["class_id"],
        "title": e["title"],
        "kind": e["kind"] or "other",
        "date": e["date"],
        "start": e["start"],
        "end": e["end"],
        "allDay": bool(e["all_day"]),
        "location": e["location"] or "",
        "notes": e["notes"] or "",
        "createdAt": e["created_at"],
    }


def serialize_class(conn, row):
    schedule = conn.execute(
        "SELECT id, day, start, \"end\", location FROM schedule_entries WHERE class_id=?",
        (row["id"],),
    ).fetchall()
    materials = conn.execute(
        "SELECT * FROM materials WHERE class_id=? ORDER BY created_at", (row["id"],)
    ).fetchall()
    notes = conn.execute(
        "SELECT * FROM notes WHERE class_id=? AND (deleted_at IS NULL OR deleted_at='') "
        "ORDER BY pinned DESC, sort_order, created_at",
        (row["id"],),
    ).fetchall()
    topics = conn.execute(
        # rowid is SQLite-only; sort_order already carries the intended order and title
        # is a deterministic tiebreak in either database.
        "SELECT * FROM syllabus_topics WHERE class_id=? ORDER BY sort_order, title",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "professor": row["professor"],
        "color": row["color"],
        "notes": row["notes"],
        "gradeScale": json.loads(row["grade_scale"]) if row["grade_scale"] else None,
        # "Quizzes 20%, best 8 of 10": a share of the grade that its items split
        "gradeCategories": [
            {"id": g["id"], "name": g["name"] or "", "weight": g["weight"],
             "dropLowest": g["drop_lowest"] or 0, "sortOrder": g["sort_order"] or 0}
            for g in conn.execute(
                "SELECT * FROM grade_categories WHERE class_id=? ORDER BY sort_order, name",
                (row["id"],)).fetchall()
        ],
        "website": row["website"] or "",
        "createdAt": row["created_at"],
        "schedule": [
            {"id": s["id"], "day": s["day"], "start": s["start"], "end": s["end"],
             "location": s["location"],
             # lecture, lab, tutorial or seminar, and the dates it actually runs
             "kind": (s["kind"] if "kind" in s.keys() else None) or "lecture",
             "section": (s["section"] if "section" in s.keys() else None) or "",
             "startDate": s["start_date"] if "start_date" in s.keys() else None,
             "endDate": s["end_date"] if "end_date" in s.keys() else None}
            for s in schedule
        ],
        "materials": [serialize_material(m, item_ids_for_material(conn, m["id"])) for m in materials],
        "notesList": [serialize_note(n) for n in notes],
        "noteFolders": [
            {
                "id": f["id"],
                "name": f["name"],
                "parentId": f["parent_id"] if "parent_id" in f.keys() else None,
                "kind": (f["kind"] or "custom") if "kind" in f.keys() else "custom",
                "sortOrder": (f["sort_order"] or 0) if "sort_order" in f.keys() else 0,
            }
            for f in conn.execute(
                "SELECT * FROM note_folders WHERE class_id=? ORDER BY sort_order, created_at",
                (row["id"],),
            ).fetchall()
        ],
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
        # a share of a grade category ("Quizzes 20%") instead of its own weight
        "categoryId": row["category_id"] if "category_id" in row.keys() else None,
        "location": (row["location"] if "location" in row.keys() else None) or "",
        "importKey": row["import_key"] if "import_key" in row.keys() else None,
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
            from docx.table import Table
            from docx.text.paragraph import Paragraph

            # Paragraphs and tables in the order they appear. Course maps and schedules
            # usually live in tables, and reading paragraphs alone loses all of it.
            d = docx.Document(filepath)
            lines = []
            for child in d.element.body.iterchildren():
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "p":
                    t = Paragraph(child, d).text.strip()
                    if t:
                        lines.append(t)
                elif tag == "tbl":
                    for row in Table(child, d).rows:
                        cells = []
                        for cell in row.cells:
                            c = " ".join(cell.text.split())
                            if c and (not cells or cells[-1] != c):   # merged cells repeat their text
                                cells.append(c)
                        if cells:
                            lines.append(" | ".join(cells))
                    lines.append("")
            return "\n".join(lines)[:200000]
        if ext in PLAIN_TEXT_EXTS:
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(400000)
            if ext in ("html", "htm"):
                text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
                text = re.sub(r"<[^>]+>", " ", text)
            return text[:200000]
    except Exception:
        return None
    return None


# Files whose words can be read straight off the disk. Without their text, search,
# link suggestions and Headstart cannot see inside them.
PLAIN_TEXT_EXTS = ("txt", "md", "markdown", "csv", "tsv", "html", "htm")


def backfill_extracted_text():
    """Read files uploaded before their type was readable, once, at startup."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, filename, stored_name FROM materials "
        "WHERE kind='file' AND (extracted_text IS NULL OR extracted_text='') AND stored_name IS NOT NULL"
    ).fetchall()
    for r in rows:
        ext = (r["filename"] or "").rsplit(".", 1)[-1].lower() if "." in (r["filename"] or "") else ""
        if ext not in PLAIN_TEXT_EXTS:
            continue
        path = os.path.join(UPLOAD_DIR, r["stored_name"])
        if os.path.exists(path):
            text = extract_text(path, r["filename"])
            if text:
                conn.execute("UPDATE materials SET extracted_text=? WHERE id=?", (text, r["id"]))
    conn.commit()
    conn.close()


backfill_extracted_text()


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
    events = conn.execute("SELECT * FROM events ORDER BY date, start").fetchall()
    term = conn.execute("SELECT * FROM term_settings WHERE id=1").fetchone()
    result = {
        "classes": [serialize_class(conn, c) for c in classes],
        "items": [serialize_item(conn, i) for i in items],
        "events": [serialize_event(e) for e in events],
        "term": {
            "name": term["name"] or "",
            "startDate": term["start_date"] or "",
            "endDate": term["end_date"] or "",
        },
        # syllabi that were read (and paid for) but not reviewed yet
        "pendingImports": [
            {"id": r["id"], "filename": r["filename"], "classId": r["class_id"], "createdAt": r["created_at"]}
            for r in conn.execute(
                "SELECT id, filename, class_id, created_at FROM syllabus_imports WHERE status='review' "
                "ORDER BY created_at DESC").fetchall()
        ],
        # a timetable pulled from SFU but not applied yet, so closing the tab does not
        # lose the review the way it would if the draft only lived in the page
        "pendingCalendarImports": [
            {"id": r["id"], "label": r["label"], "source": r["source"],
             "classId": r["class_id"], "createdAt": r["created_at"]}
            for r in conn.execute(
                "SELECT id, label, source, class_id, created_at FROM calendar_imports "
                "WHERE status='review' ORDER BY created_at DESC").fetchall()
        ],
        # Notes and files jotted down or dropped in before there was anywhere to put
        # them. They live outside every class until they are filed.
        "unfiled": {
            "notesList": [serialize_note(n) for n in conn.execute(
                "SELECT * FROM notes WHERE class_id IS NULL "
                "ORDER BY pinned DESC, updated_at DESC").fetchall()],
            "materials": [serialize_material(m, item_ids_for_material(conn, m["id"])) for m in conn.execute(
                "SELECT * FROM materials WHERE class_id IS NULL "
                "ORDER BY created_at DESC").fetchall()],
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
        "INSERT INTO classes (id, code, name, professor, color, notes, grade_scale, website, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            cid,
            data.get("code", ""),
            data.get("name", ""),
            data.get("professor", ""),
            data.get("color", ""),
            data.get("notes", ""),
            json.dumps(data["gradeScale"]) if data.get("gradeScale") else None,
            data.get("website", ""),
            now,
        ),
    )
    for s in data.get("schedule", []) or []:
        conn.execute(
            "INSERT INTO schedule_entries (id, class_id, day, start, \"end\", location, kind, section, start_date, end_date)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), cid, s.get("day"), s.get("start"), s.get("end"), s.get("location", ""),
             s.get("kind") or "lecture", s.get("section") or "", s.get("startDate"), s.get("endDate")),
        )
    conn.commit()
    conn.close()
    return jsonify({"id": cid}), 201


def save_grade_categories(conn, cid, cats):
    """Replace a class's grade categories, keeping ids so items stay in their category.

    A category that is removed releases its items rather than deleting them.
    """
    keep = []
    for i, g in enumerate(cats):
        gid = g.get("id") or str(uuid.uuid4())
        keep.append(gid)
        exists = conn.execute("SELECT 1 FROM grade_categories WHERE id=? AND class_id=?", (gid, cid)).fetchone()
        values = (g.get("name") or "", g.get("weight"), int(g.get("dropLowest") or 0), i)
        if exists:
            conn.execute("UPDATE grade_categories SET name=?, weight=?, drop_lowest=?, sort_order=? WHERE id=?",
                         values + (gid,))
        else:
            conn.execute("INSERT INTO grade_categories (id, class_id, name, weight, drop_lowest, sort_order, created_at)"
                         " VALUES (?,?,?,?,?,?,?)", (gid, cid) + values + (datetime.utcnow().isoformat(),))
    gone = [r["id"] for r in conn.execute("SELECT id FROM grade_categories WHERE class_id=?", (cid,)).fetchall()
            if r["id"] not in keep]
    for gid in gone:
        conn.execute("UPDATE items SET category_id=NULL WHERE category_id=?", (gid,))
        conn.execute("DELETE FROM grade_categories WHERE id=?", (gid,))
    return keep


@app.route("/api/classes/<cid>", methods=["PUT"])
def update_class(cid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    colmap = {"code": "code", "name": "name", "professor": "professor", "color": "color", "notes": "notes", "website": "website"}
    fields, values = [], []
    for key, col in colmap.items():
        if key in data:
            fields.append(f"{col}=?")
            values.append(data[key])
    if "gradeScale" in data:
        fields.append("grade_scale=?")
        values.append(json.dumps(data["gradeScale"]) if data["gradeScale"] else None)
    if fields:
        values.append(cid)
        conn.execute(f"UPDATE classes SET {', '.join(fields)} WHERE id=?", values)
    if "gradeCategories" in data:
        save_grade_categories(conn, cid, data["gradeCategories"] or [])
    if "schedule" in data:
        conn.execute("DELETE FROM schedule_entries WHERE class_id=?", (cid,))
        for s in data["schedule"] or []:
            conn.execute(
                "INSERT INTO schedule_entries (id, class_id, day, start, \"end\", location, kind, section, start_date, end_date)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), cid, s.get("day"), s.get("start"), s.get("end"), s.get("location", ""),
                 s.get("kind") or "lecture", s.get("section") or "", s.get("startDate"), s.get("endDate")),
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
        """INSERT INTO items (id, class_id, title, type, due_date, due_time, status, completed_at, weight, score, notes, created_at,
                              category_id, location, import_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            data.get("categoryId"),
            data.get("location") or "",
            data.get("importKey"),
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
        "categoryId": "category_id",
        "location": "location",
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

@app.route("/api/materials", methods=["POST"])
def add_unfiled_material():
    """A file dropped in before it has been filed under a class."""
    return add_material(None)


@app.route("/api/classes/<cid>/materials", methods=["POST"])
def add_material(cid):
    conn = get_db()
    if cid is not None:
        cls = conn.execute("SELECT id FROM classes WHERE id=?", (cid,)).fetchone()
        if not cls:
            conn.close()
            abort(404)
    now = datetime.utcnow().isoformat()
    mid = str(uuid.uuid4())
    # a file can belong to one assignment as well as the class
    item_id = request.form.get("itemId") or (request.get_json(silent=True) or {}).get("itemId") or None

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
            """INSERT INTO materials (id, class_id, item_id, category, title, kind, url, filename, stored_name, mimetype, size, extracted_text, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, cid, None, category, title, "file", None, original, stored, mimetype, size, text, now),
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
            """INSERT INTO materials (id, class_id, item_id, category, title, kind, url, filename, stored_name, mimetype, size, extracted_text, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, cid, None, category, title, "link", url, None, None, None, None, None, now),
        )
    if item_id and conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
        conn.execute(
            "INSERT INTO item_files (id, item_id, material_id, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT DO NOTHING",
            (str(uuid.uuid4()), item_id, mid, now))
    conn.commit()
    conn.close()
    return jsonify({"id": mid}), 201


@app.route("/api/items/<iid>/description", methods=["PUT"])
def update_item_description(iid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    conn.execute("UPDATE items SET notes=? WHERE id=?", (data.get("notes") or "", iid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


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
    # attaching a file to an assignment (adds a link), or detaching it from all of them
    if "itemId" in data:
        if data["itemId"]:
            conn.execute(
                "INSERT INTO item_files (id, item_id, material_id, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT DO NOTHING",
                (str(uuid.uuid4()), data["itemId"], mid, datetime.utcnow().isoformat()))
        else:
            conn.execute("DELETE FROM item_files WHERE material_id=?", (mid,))
    # filing it under a class, or back into the Inbox. Its assignment links survive
    # the move: a file's home and what it is used for are separate questions.
    if "classId" in data:
        fields.append("class_id=?")
        values.append(data["classId"] or None)
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


# Browsers only render a file inline (PDF viewer, image, plain text) when the
# response is not marked as an attachment, so previewing is the default and an
# actual download is opt-in with ?download=1.
INLINE_MIMETYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
}


def material_mimetype(m):
    """Best guess at the stored file's content type."""
    mt = (m["mimetype"] or "").split(";")[0].strip().lower()
    if not mt or mt == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(m["filename"] or m["stored_name"] or "")
        mt = (guessed or mt or "application/octet-stream").lower()
    return mt


@app.route("/api/materials/<mid>/download")
def download_material(mid):
    conn = get_db()
    m = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    conn.close()
    if not m or m["kind"] != "file":
        abort(404)
    mt = material_mimetype(m)
    forced = request.args.get("download") in ("1", "true", "yes")
    inline = not forced and mt in INLINE_MIMETYPES
    return send_from_directory(
        UPLOAD_DIR,
        m["stored_name"],
        mimetype=mt,
        as_attachment=not inline,
        download_name=m["filename"],
    )


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



@app.route("/api/notes", methods=["POST"])
def add_unfiled_note():
    """A note with nowhere to live yet. File it under a class whenever you like."""
    return add_note(None)


@app.route("/api/classes/<cid>/notes", methods=["POST"])
def add_note(cid):
    data = request.get_json(force=True) or {}
    text = data.get("text") or ""
    title = (data.get("title") or "").strip()
    # A brand new note legitimately has neither yet; the interface shows a
    # placeholder until the person types something.
    conn = get_db()
    nid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO notes (id, class_id, title, folder_id, text, linked_item_id, updated_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (nid, cid, title, data.get("folderId"), text,
         # Only an explicit link. Creating a note from inside an assignment says what
         # it belongs to; anything else is offered as a suggestion, never assumed.
         data.get("linkedItemId") or None, now, now),
    )
    conn.commit()
    conn.close()
    return jsonify({"id": nid}), 201


@app.route("/api/notes/<nid>", methods=["PUT"])
def update_note(nid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    row = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    fields, values = [], []
    for key, col in (("title", "title"), ("text", "text"), ("folderId", "folder_id")):
        if key in data:
            fields.append(f"{col}=?")
            values.append(data[key])
    # filing an unfiled note under a class, or taking it back out
    if "classId" in data:
        fields.append("class_id=?")
        values.append(data["classId"] or None)
        if not data["classId"]:
            fields.append("folder_id=?")     # a folder belongs to a class
            values.append(None)
    # attaching the note to an assignment, or taking it off one
    if "linkedItemId" in data:
        fields.append("linked_item_id=?")
        values.append(data["linkedItemId"] or None)
    for key, col in (("pinned", "pinned"), ("starred", "starred")):
        if key in data:
            fields.append(f"{col}=?")
            values.append(1 if data[key] else 0)
    if "sortOrder" in data:
        fields.append("sort_order=?")
        values.append(int(data.get("sortOrder") or 0))
    if "deletedAt" in data:
        fields.append("deleted_at=?")
        values.append(data["deletedAt"])
    # Snapshot the previous text, at most once every five minutes, so autosave
    # does not fill the table with near-identical rows.
    if "text" in data and (row["text"] or "") != (data.get("text") or ""):
        last = conn.execute(
            "SELECT created_at FROM note_versions WHERE note_id=? ORDER BY created_at DESC LIMIT 1",
            (nid,),
        ).fetchone()
        fresh = False
        if last and last["created_at"]:
            try:
                fresh = (datetime.utcnow() - datetime.fromisoformat(last["created_at"])).total_seconds() < 300
            except Exception:
                fresh = False
        if not fresh:
            conn.execute(
                "INSERT INTO note_versions (id, note_id, title, text, created_at) VALUES (?,?,?,?,?)",
                (str(uuid.uuid4()), nid, row["title"], row["text"], datetime.utcnow().isoformat()),
            )
            conn.execute(
                "DELETE FROM note_versions WHERE note_id=? AND id NOT IN "
                "(SELECT id FROM note_versions WHERE note_id=? ORDER BY created_at DESC LIMIT 50)",
                (nid, nid),
            )
    fields.append("updated_at=?")
    values.append(datetime.utcnow().isoformat())
    values.append(nid)
    conn.execute(f"UPDATE notes SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/classes/<cid>/note-folders", methods=["POST"])
def add_note_folder(cid):
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip() or "New folder"
    kind = data.get("kind") or "custom"
    if kind not in ("lecture", "reading", "exam", "assignment", "custom"):
        kind = "custom"
    parent_id = data.get("parentId") or None
    fid = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO note_folders (id, class_id, parent_id, name, kind, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (fid, cid, parent_id, name, kind, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return jsonify({"id": fid}), 201


@app.route("/api/note-folders/<fid>", methods=["PUT"])
def rename_note_folder(fid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    fields, values = [], []
    if "name" in data:
        fields.append("name=?")
        values.append((data.get("name") or "").strip() or "Untitled")
    if "kind" in data:
        kind = data.get("kind") or "custom"
        if kind not in ("lecture", "reading", "exam", "assignment", "custom"):
            kind = "custom"
        fields.append("kind=?")
        values.append(kind)
    if "sortOrder" in data:
        fields.append("sort_order=?")
        values.append(int(data.get("sortOrder") or 0))
    if "parentId" in data:
        parent_id = data.get("parentId") or None
        if parent_id == fid or folder_would_cycle(conn, fid, parent_id):
            conn.close()
            return jsonify({"error": "That would put a folder inside one of its own subfolders."}), 400
        fields.append("parent_id=?")
        values.append(parent_id)
    if not fields:
        conn.close()
        return jsonify({"ok": True})
    values.append(fid)
    conn.execute(f"UPDATE note_folders SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/note-folders/<fid>", methods=["DELETE"])
def delete_note_folder(fid):
    conn = get_db()
    # notes in the folder survive; they just move back to the top level
    conn.execute("UPDATE notes SET folder_id=NULL WHERE folder_id=?", (fid,))
    conn.execute("DELETE FROM note_folders WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/notes/<nid>", methods=["DELETE"])
def delete_note(nid):
    """Move to the Trash by default. ?permanent=1 actually destroys it."""
    conn = get_db()
    if request.args.get("permanent") == "1":
        conn.execute("DELETE FROM notes WHERE id=?", (nid,))
    else:
        conn.execute("UPDATE notes SET deleted_at=? WHERE id=?",
                     (datetime.utcnow().isoformat(), nid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/notes/trash")
def list_trashed_notes():
    conn = get_db()
    rows = conn.execute(
        "SELECT n.*, c.code AS class_code FROM notes n LEFT JOIN classes c ON c.id=n.class_id "
        "WHERE n.deleted_at IS NOT NULL AND n.deleted_at<>'' ORDER BY n.deleted_at DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = serialize_note(r)
        d["classId"] = r["class_id"]
        d["classCode"] = r["class_code"] or ""
        out.append(d)
    conn.close()
    return jsonify(out)


@app.route("/api/notes/<nid>/restore", methods=["POST"])
def restore_note(nid):
    conn = get_db()
    conn.execute("UPDATE notes SET deleted_at=NULL WHERE id=?", (nid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/notes/<nid>/duplicate", methods=["POST"])
def duplicate_note(nid):
    conn = get_db()
    row = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    new_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO notes (id, class_id, title, folder_id, text, linked_item_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (new_id, row["class_id"], ((row["title"] or "Untitled note") + " copy"),
         row["folder_id"], row["text"], row["linked_item_id"], now, now),
    )
    # carry the connections across too, or the copy is only half a copy
    for link in conn.execute("SELECT * FROM note_links WHERE note_id=?", (nid,)).fetchall():
        conn.execute(
            "INSERT INTO note_links (id, note_id, item_id, file_id, event_id, schedule_entry_id, "
            "headstart_id, target_note_id, label, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), new_id, link["item_id"], link["file_id"], link["event_id"],
             link["schedule_entry_id"],
             link["headstart_id"] if "headstart_id" in link.keys() else None,
             link["target_note_id"], link["label"], now),
        )
    conn.commit()
    conn.close()
    return jsonify({"id": new_id}), 201


@app.route("/api/notes/<nid>/versions")
def list_note_versions(nid):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, text, created_at FROM note_versions WHERE note_id=? "
        "ORDER BY created_at DESC LIMIT 50",
        (nid,),
    ).fetchall()
    out = [{"id": r["id"], "title": r["title"] or "", "text": r["text"] or "",
            "createdAt": r["created_at"]} for r in rows]
    conn.close()
    return jsonify(out)


@app.route("/api/notes/<nid>/versions/<vid>/restore", methods=["POST"])
def restore_note_version(nid, vid):
    conn = get_db()
    v = conn.execute("SELECT * FROM note_versions WHERE id=? AND note_id=?", (vid, nid)).fetchone()
    cur = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    if not v or not cur:
        conn.close()
        abort(404)
    now = datetime.utcnow().isoformat()
    # the version being replaced becomes a version itself, so this is undoable
    conn.execute(
        "INSERT INTO note_versions (id, note_id, title, text, created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), nid, cur["title"], cur["text"], now),
    )
    conn.execute("UPDATE notes SET title=?, text=?, updated_at=? WHERE id=?",
                 (v["title"], v["text"], now, nid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/notes/<nid>/links", methods=["GET", "POST"])
def note_links(nid):
    conn = get_db()
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        kind = data.get("type")
        col = {"item": "item_id", "file": "file_id", "event": "event_id",
               "lecture": "schedule_entry_id", "headstart": "headstart_id",
               "note": "target_note_id"}.get(kind)
        if not col or not data.get("id"):
            conn.close()
            return jsonify({"error": "A link needs a type and an id."}), 400
        existing = conn.execute(
            f"SELECT id FROM note_links WHERE note_id=? AND {col}=?", (nid, data["id"])
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({"id": existing["id"]}), 200
        lid = str(uuid.uuid4())
        conn.execute(
            f"INSERT INTO note_links (id, note_id, {col}, label, created_at) VALUES (?,?,?,?,?)",
            (lid, nid, data["id"], data.get("label") or "", datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        return jsonify({"id": lid}), 201
    rows = conn.execute("SELECT * FROM note_links WHERE note_id=? ORDER BY created_at", (nid,)).fetchall()
    out = [{"id": r["id"], "itemId": r["item_id"], "fileId": r["file_id"],
            "eventId": r["event_id"], "lectureId": r["schedule_entry_id"],
            "headstartId": r["headstart_id"] if "headstart_id" in r.keys() else None,
            "noteId": r["target_note_id"], "label": r["label"] or ""} for r in rows]
    conn.close()
    return jsonify(out)


@app.route("/api/note-links/<lid>", methods=["DELETE"])
def delete_note_link(lid):
    conn = get_db()
    conn.execute("DELETE FROM note_links WHERE id=?", (lid,))
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

@app.route("/api/events", methods=["POST"])
def create_event():
    data = request.get_json(force=True) or {}
    conn = get_db()
    eid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO events (id, class_id, title, kind, date, start, \"end\", all_day, location, notes, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            eid,
            data.get("classId") or None,
            data.get("title", ""),
            data.get("kind", "other"),
            data.get("date"),
            data.get("start"),
            data.get("end"),
            1 if data.get("allDay") else 0,
            data.get("location", ""),
            data.get("notes", ""),
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"id": eid}), 201


@app.route("/api/events/<eid>", methods=["PUT"])
def update_event(eid):
    data = request.get_json(force=True) or {}
    colmap = {
        "classId": "class_id", "title": "title", "kind": "kind", "date": "date",
        # the SQL name is quoted because `end` is reserved in Postgres
        "start": "start", "end": '"end"', "location": "location", "notes": "notes",
    }
    fields, values = [], []
    for key, col in colmap.items():
        if key in data:
            fields.append(f"{col}=?")
            values.append(data[key] or None if key == "classId" else data[key])
    if "allDay" in data:
        fields.append("all_day=?")
        values.append(1 if data["allDay"] else 0)
    conn = get_db()
    if fields:
        values.append(eid)
        conn.execute(f"UPDATE events SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/events/<eid>", methods=["DELETE"])
def delete_event(eid):
    conn = get_db()
    conn.execute("DELETE FROM events WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# Times in the feed are Vancouver wall-clock, not floating. Emitted bare, "14:30" is
# whatever the reading calendar decides it is, and a weekly class meeting slips by an
# hour the moment daylight time ends in November. TZID pins it; the VTIMEZONE block is
# carried because strict readers will not resolve a bare IANA name.
CAL_TZ = "America/Vancouver"
VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    f"TZID:{CAL_TZ}",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:-0800", "TZOFFSETTO:-0700", "TZNAME:PDT",
    "DTSTART:19700308T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:-0700", "TZOFFSETTO:-0800", "TZNAME:PST",
    "DTSTART:19701101T020000", "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
]


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

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Vesta//EN", "CALSCALE:GREGORIAN"] + VTIMEZONE
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
                    f"DTSTART;TZID={CAL_TZ}:{dtstart}",
                    f"DTEND;TZID={CAL_TZ}:{dtend}",
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
            lines.append(f"DTSTART;TZID={CAL_TZ}:{d.strftime('%Y%m%dT%H%M%S')}")
            lines.append(f"DTEND;TZID={CAL_TZ}:{dend.strftime('%Y%m%dT%H%M%S')}")
        else:
            # An all-day DTEND is exclusive: a one-day event ends on the following day.
            # Equal DTSTART and DTEND is a zero-length event, which readers either drop
            # or render on the wrong day.
            start_d = datetime.strptime(it["due_date"], "%Y-%m-%d")
            lines.append(f"DTSTART;VALUE=DATE:{start_d.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(start_d + timedelta(days=1)).strftime('%Y%m%d')}")
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


@app.route("/health")
def health():
    """What this instance actually is, without revealing anything private.

    Exists because the sign-in screen looks identical whether the app is talking to
    Postgres or quietly falling back to a throwaway SQLite file, and because a volume
    that failed to mount is invisible until the day a deploy eats your uploads.
    """
    import db as _db
    import auth as _auth
    uploads_ok = os.access(UPLOAD_DIR, os.W_OK)
    out = {
        "ok": True,
        "database": "postgres" if _db.DATABASE_URL else "sqlite",
        "accounts": _auth.enabled(),
        "dataDir": _db.DATA_DIR,
        "uploadsWritable": uploads_ok,
        # Railway sets this only when a volume is actually mounted, so it is the
        # difference between "configured" and "really there".
        "volumeMountedAt": os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"),
        # Which commit is actually running. Without this, "is the fix deployed yet?"
        # can only be answered by guessing from behaviour.
        "version": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:7] or None,
    }
    if _db.DATABASE_URL:
        try:
            conn = _db.get_db(user_id=None)
            conn.as_owner()
            row = conn.execute(
                "select count(*) as n from pg_class c join pg_namespace ns"
                " on ns.oid = c.relnamespace where ns.nspname='public' and c.relkind='r'"
            ).fetchone()
            out["tables"] = row["n"] if row else 0
            conn.close()
        except Exception as e:
            out["ok"] = False
            out["databaseError"] = str(e)[:200]
    return jsonify(out)


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "File is larger than 25 MB."}), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")

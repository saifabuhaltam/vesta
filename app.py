import os
import re
import shutil
import mimetypes
import json
import subprocess
import threading
import uuid
from datetime import datetime, timedelta

import anthropic
from flask import Flask, request, jsonify, send_from_directory, abort, Response, session
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


from db import (get_db, init_db, UPLOAD_DIR, SEMESTER_SCOPED, for_each_account,
                current_user_id,
                active_semester, active_semester_id, set_active_semester,
                default_term_name, semester_for, SEMESTER_ORDER,
                ensure_default_file_folders, folder_id_for_kind, folder_descendants,
                CATEGORY_TO_FOLDER_KIND)



app = Flask(__name__, static_folder="static", static_url_path="")

# The AI layer lives in its own module: Headstart tools, Quiz Me, flashcards and
# practice tests, plus the cost guardrails they all share.
from ai import bp as ai_bp  # noqa: E402
app.register_blueprint(ai_bp)
from links import bp as links_bp  # noqa: E402
app.register_blueprint(links_bp)
# Continuing conversations: pinned sources plus history, beside the one-shot tools.
from threads import bp as threads_bp  # noqa: E402
app.register_blueprint(threads_bp)
# Study's Humanizer: rewrites AI-sounding prose and marks the habits it removed.
from humanizer import bp as humanizer_bp  # noqa: E402
app.register_blueprint(humanizer_bp)
# syllabus import reads uploads with the same extractor the rest of the app uses
# looked up at call time: extract_text is defined further down this file
app.config["EXTRACT_TEXT"] = lambda path, name: extract_text(path, name)
from syllabus_import import bp as syllabus_bp  # noqa: E402
app.register_blueprint(syllabus_bp)
# calendar: SFU's published timetable now, connected calendars next
from calendar_api import bp as calendar_bp  # noqa: E402
app.register_blueprint(calendar_bp)
# Per-account preferences: everything the Settings screen writes.
from prefs import bp as prefs_bp  # noqa: E402
app.register_blueprint(prefs_bp)
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


def rows_in(conn, sql, ids, chunk=300):
    """Run one query over a list of ids, in chunks, and return every row.

    `sql` carries `{marks}` where the placeholders belong. Chunking keeps a term with
    hundreds of assignments from building a single statement with hundreds of
    parameters, which some drivers handle badly.
    """
    ids = [i for i in ids if i is not None]
    out = []
    for start in range(0, len(ids), chunk):
        part = ids[start:start + chunk]
        marks = ",".join("?" * len(part))
        out.extend(conn.execute(sql.format(marks=marks), tuple(part)).fetchall())
    return out


def group_by(rows, key):
    """Rows bucketed by one column, keeping the order the query returned them in."""
    out = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def state_children(conn, class_ids, item_ids, extra_material_ids=()):
    """Every child row `/api/state` needs, one query per table instead of per parent.

    Serialising a class cost seven queries and an item four, plus one per file for its
    assignment links, so a term with five classes, sixty assignments and forty files
    ran well over three hundred round trips for a single page load. Against SQLite on
    the same disk that is invisible; against Postgres over a network it is seconds, and
    the page reloads all of this after every small edit. Same rows, same order, one
    query each.
    """
    by_class = lambda sql: rows_in(conn, sql, class_ids)
    by_item = lambda sql: rows_in(conn, sql, item_ids)
    materials = by_class(
        "SELECT * FROM materials WHERE class_id IN ({marks}) ORDER BY created_at")
    # The files' assignment links are looked up by material, and the unfiled files
    # (Inbox) need theirs too, so they come in on the same query.
    material_ids = [m["id"] for m in materials] + list(extra_material_ids)
    return {
        "schedule": group_by(by_class(
            'SELECT * FROM schedule_entries WHERE class_id IN ({marks})'), "class_id"),
        "materials": group_by(materials, "class_id"),
        "notes": group_by(by_class(
            "SELECT * FROM notes WHERE class_id IN ({marks})"
            " AND (deleted_at IS NULL OR deleted_at='')"
            " ORDER BY pinned DESC, sort_order, created_at"), "class_id"),
        "topics": group_by(by_class(
            "SELECT * FROM syllabus_topics WHERE class_id IN ({marks})"
            " ORDER BY sort_order, title"), "class_id"),
        "gradeCategories": group_by(by_class(
            "SELECT * FROM grade_categories WHERE class_id IN ({marks})"
            " ORDER BY sort_order, name"), "class_id"),
        "fileFolders": group_by(by_class(
            "SELECT * FROM file_folders WHERE class_id IN ({marks})"
            " ORDER BY sort_order, created_at"), "class_id"),
        "noteFolders": group_by(by_class(
            "SELECT * FROM note_folders WHERE class_id IN ({marks})"
            " ORDER BY sort_order, created_at"), "class_id"),
        "subtasks": group_by(by_item(
            "SELECT * FROM subtasks WHERE item_id IN ({marks})"), "item_id"),
        "headstarts": group_by(by_item(
            "SELECT id, item_id, kind, status, updated_at FROM headstarts"
            " WHERE item_id IN ({marks}) ORDER BY updated_at DESC"), "item_id"),
        "rubrics": group_by(by_item(
            "SELECT * FROM rubrics WHERE item_id IN ({marks})"), "item_id"),
        "itemFiles": group_by(rows_in(
            conn,
            "SELECT material_id, item_id FROM item_files WHERE material_id IN ({marks})"
            " ORDER BY created_at", material_ids), "material_id"),
        # Notes point at assignments two ways: the `linked_item_id` column, and
        # `note_links` rows from the Links menu. The card reads both now.
        "noteLinks": group_by(by_item(
            "SELECT note_id, item_id FROM note_links WHERE item_id IN ({marks})"), "note_id"),
    }


def note_link_item_ids(pre, nid):
    """Assignments this note points at through `note_links`."""
    if pre is None:
        return []
    return [r["item_id"] for r in pre["noteLinks"].get(nid, []) if r["item_id"]]


def material_item_ids(pre, mid, conn=None):
    """The assignments a file is attached to, from the prefetched bundle or the database."""
    if pre is not None:
        return [r["item_id"] for r in pre["itemFiles"].get(mid, [])]
    return item_ids_for_material(conn, mid)


def serialize_material(m, item_ids=None):
    item_ids = item_ids or []
    d = {
        "id": m["id"],
        "itemIds": item_ids,
        "itemId": item_ids[0] if item_ids else None,   # older callers read a single id
        "category": m["category"],
        "folderId": m["folder_id"] if "folder_id" in m.keys() else None,
        "title": m["title"],
        "kind": m["kind"],
        "hasText": bool(m["extracted_text"]),
        "createdAt": m["created_at"],
    }
    if m["kind"] == "file":
        keys = m.keys()
        d["filename"] = m["filename"]
        d["size"] = m["size"]
        d["mimetype"] = m["mimetype"]
        d["url"] = f"/api/materials/{m['id']}/download"
        # An Office file gets a converted PDF the browser can actually render.
        # previewStatus lets the page say "converting…" instead of showing nothing.
        status = (m["preview_status"] if "preview_status" in keys else None) or ""
        d["previewStatus"] = status
        if status == "ready" and ("preview_name" in keys) and m["preview_name"]:
            d["previewUrl"] = f"/api/materials/{m['id']}/preview"
        # the row can outlive the upload (a deleted class removes the file), and
        # a preview that says so beats an empty frame
        d["missing"] = not (m["stored_name"] and os.path.exists(os.path.join(UPLOAD_DIR, m["stored_name"])))
    else:
        d["url"] = m["url"]
    return d


def serialize_note(n, link_item_ids=None):
    """One note.

    `linkedItemId` is the single column written when a note is made from an
    assignment. `linkedItemIds` is every assignment the note points at through
    `note_links`, which the Links menu writes and which the assignment card used to
    ignore entirely: a note linked to two assignments showed up on neither.
    """
    keys = n.keys()
    linked = list(link_item_ids or [])
    if n["linked_item_id"] and n["linked_item_id"] not in linked:
        linked.insert(0, n["linked_item_id"])
    return {
        "id": n["id"],
        "title": n["title"] or "",
        "folderId": n["folder_id"],
        "text": n["text"],
        "linkedItemId": n["linked_item_id"],
        "linkedItemIds": linked,
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
        # Where it came from, and whether the page may offer to edit it. An event
        # mirrored from a Google calendar belongs to Google: the way to change it is to
        # change it there, and Vesta would only overwrite the edit on the next sync.
        "source": (e["source"] if "source" in e.keys() else "") or "",
        "readOnly": bool(e["read_only"] if "read_only" in e.keys() else 0),
    }


def serialize_class(conn, row, pre=None):
    """One class, with its schedule, files, notes and syllabus.

    `pre` is the prefetched bundle from `state_children`. Without it each class costs
    seven queries of its own, which is why `/api/state` always passes one.
    """
    cid = row["id"]
    if pre is not None:
        schedule = pre["schedule"].get(cid, [])
        materials = pre["materials"].get(cid, [])
        notes = pre["notes"].get(cid, [])
        topics = pre["topics"].get(cid, [])
        grade_cats = pre["gradeCategories"].get(cid, [])
        file_folders = pre["fileFolders"].get(cid, [])
        note_folders = pre["noteFolders"].get(cid, [])
    else:
        schedule = conn.execute(
            'SELECT * FROM schedule_entries WHERE class_id=?', (cid,)).fetchall()
        materials = conn.execute(
            "SELECT * FROM materials WHERE class_id=? ORDER BY created_at", (cid,)
        ).fetchall()
        notes = conn.execute(
            "SELECT * FROM notes WHERE class_id=? AND (deleted_at IS NULL OR deleted_at='') "
            "ORDER BY pinned DESC, sort_order, created_at",
            (cid,),
        ).fetchall()
        topics = conn.execute(
            # rowid is SQLite-only; sort_order already carries the intended order and title
            # is a deterministic tiebreak in either database.
            "SELECT * FROM syllabus_topics WHERE class_id=? ORDER BY sort_order, title",
            (cid,),
        ).fetchall()
        grade_cats = conn.execute(
            "SELECT * FROM grade_categories WHERE class_id=? ORDER BY sort_order, name",
            (cid,)).fetchall()
        file_folders = conn.execute(
            "SELECT * FROM file_folders WHERE class_id=? ORDER BY sort_order, created_at",
            (cid,)).fetchall()
        note_folders = conn.execute(
            "SELECT * FROM note_folders WHERE class_id=? ORDER BY sort_order, created_at",
            (cid,)).fetchall()
    return {
        "id": row["id"],
        # Which term it belongs to, so the class form can show where it sits and move it.
        "semesterId": row["semester_id"] if "semester_id" in row.keys() else None,
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
            for g in grade_cats
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
        "materials": [serialize_material(m, material_item_ids(pre, m["id"], conn)) for m in materials],
        "fileFolders": [serialize_file_folder(f) for f in file_folders],
        "notesList": [serialize_note(n, note_link_item_ids(pre, n["id"])) for n in notes],
        "noteFolders": [
            {
                "id": f["id"],
                "name": f["name"],
                "parentId": f["parent_id"] if "parent_id" in f.keys() else None,
                "kind": (f["kind"] or "custom") if "kind" in f.keys() else "custom",
                "sortOrder": (f["sort_order"] or 0) if "sort_order" in f.keys() else 0,
            }
            for f in note_folders
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


def serialize_item(conn, row, pre=None):
    """One assignment, with its subtasks, saved generations and rubric.

    `pre` is the prefetched bundle from `state_children`; see `serialize_class`.
    """
    if pre is not None:
        subtasks = pre["subtasks"].get(row["id"], [])
        headstarts = pre["headstarts"].get(row["id"], [])
        rubrics = pre["rubrics"].get(row["id"], [])
        rubric_row = rubrics[0] if rubrics else None
    else:
        subtasks = conn.execute(
            "SELECT * FROM subtasks WHERE item_id=?", (row["id"],)
        ).fetchall()
        headstarts = conn.execute(
            "SELECT id, kind, status, updated_at FROM headstarts WHERE item_id=? ORDER BY updated_at DESC",
            (row["id"],)
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
        # id and updatedAt travel with the summary so the interface can open a saved
        # result; the text itself does not, because every item's every generation on
        # every state load would be a large payload for something rarely opened.
        # `GET /api/items/<id>/headstarts` fetches the content when one is clicked.
        "headstarts": [{"id": h["id"], "kind": h["kind"], "status": h["status"],
                        "updatedAt": h["updated_at"]} for h in headstarts],
        "rubric": serialize_rubric(rubric_row),
    }


def extract_pdf_text(filepath):
    """Read a PDF's text, or None. Shared by uploads and by Office conversions.

    PowerPoint, Excel and the older Word formats have no reader here, but LibreOffice
    already converts them to PDF for the preview pane, so that PDF is what gets read.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(filepath)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text[:200000]
    except Exception:
        return None


def extract_text(filepath, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if ext == "pdf":
            return extract_pdf_text(filepath)
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
    """Read files uploaded before their type was readable, once, at startup.

    Run once per account. A single ownerless connection reads zero rows on Postgres,
    because forced RLS compares `user_id` against a null `auth.uid()`.
    """
    for_each_account(_backfill_extracted_text_for)


def _backfill_extracted_text_for(uid):
    conn = get_db(user_id=uid)
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


# ---------------- semesters ----------------

# Unlocking an archived term is deliberately per browser session rather than a stored
# flag: it lasts while you are working and is gone the next time, which is the right
# default for a record you are only meant to read.
UNLOCK_KEY = "unlocked_semester"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Managing terms has to keep working while a term is locked, or unlocking one would
# require unlocking it first.
UNGUARDED_PREFIXES = ("/api/semesters", "/api/auth")


def serialize_semester(r):
    return {
        "id": r["id"],
        "name": r["name"] or "",
        "startDate": r["start_date"] or "",
        "endDate": r["end_date"] or "",
        "status": r["status"] or "active",
        "archivedAt": r["archived_at"] or "",
        "createdAt": r["created_at"] or "",
    }


def semester_counts(conn, sid):
    """What a term holds, for the switcher and for refusing to delete it."""
    n = lambda sql: conn.execute(sql, (sid,)).fetchone()["n"]
    return {
        "classes": n("SELECT COUNT(*) n FROM classes WHERE semester_id=?"),
        "items": n("SELECT COUNT(*) n FROM items WHERE semester_id=?"),
        "notes": n("SELECT COUNT(*) n FROM notes WHERE semester_id=? AND (deleted_at IS NULL OR deleted_at='')"),
        "materials": n("SELECT COUNT(*) n FROM materials WHERE semester_id=?"),
    }


@app.before_request
def _guard_archived_semester():
    """Refuse writes to an archived term unless this session has unlocked it.

    Enforced here rather than in the page, because a read-only record that is only
    read-only in the interface is not read-only. Registered after the accounts gate,
    so g.user_id is already set and the connection below is the right student's.
    """
    if request.method not in WRITE_METHODS or not request.path.startswith("/api/"):
        return None
    if request.path.startswith(UNGUARDED_PREFIXES):
        return None
    conn = get_db()
    try:
        sem = active_semester(conn)
        sid, status, name = sem["id"], sem["status"], sem["name"]
    finally:
        conn.close()
    if status != "archived" or session.get(UNLOCK_KEY) == sid:
        return None
    return jsonify({
        "error": f"{name} is archived. Unlock it to make changes.",
        "archived": True, "semesterId": sid, "semester": name,
    }), 423


@app.route("/api/semesters", methods=["GET", "POST"])
def semesters():
    conn = get_db()
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        sid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO semesters (id, name, start_date, end_date, status, created_at)"
            " VALUES (?,?,?,?,'active',?)",
            (sid, (data.get("name") or "").strip() or default_term_name(),
             data.get("startDate") or "", data.get("endDate") or "",
             datetime.utcnow().isoformat()))
        conn.commit()
        # A new term is the one you want to be looking at: that is the whole point of
        # starting it. Old work stays exactly where it is, one switch away.
        if data.get("makeActive", True):
            set_active_semester(conn, sid)
            session.pop(UNLOCK_KEY, None)
        row = conn.execute("SELECT * FROM semesters WHERE id=?", (sid,)).fetchone()
        out = serialize_semester(row)
        conn.close()
        return jsonify(out), 201

    active = active_semester(conn)
    rows = conn.execute(f"SELECT * FROM semesters ORDER BY {SEMESTER_ORDER}").fetchall()
    out = []
    for r in rows:
        d = serialize_semester(r)
        d["counts"] = semester_counts(conn, r["id"])
        d["active"] = r["id"] == active["id"]
        out.append(d)
    conn.close()
    return jsonify(out)


@app.route("/api/semesters/active", methods=["PUT"])
def switch_semester():
    """Point the whole app at another term."""
    data = request.get_json(force=True) or {}
    sid = data.get("id") or ""
    conn = get_db()
    row = conn.execute("SELECT * FROM semesters WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "No such semester."}), 404
    set_active_semester(conn, sid)
    # Switching away re-locks whatever was unlocked, including the term being left.
    session.pop(UNLOCK_KEY, None)
    out = serialize_semester(row)
    conn.close()
    return jsonify(out)


@app.route("/api/semesters/<sid>", methods=["PUT", "DELETE"])
def semester(sid):
    conn = get_db()
    row = conn.execute("SELECT * FROM semesters WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "No such semester."}), 404

    if request.method == "DELETE":
        counts = semester_counts(conn, sid)
        if any(counts.values()):
            conn.close()
            # Deleting would take real coursework with it. Archiving is what the
            # student actually wants here, and it is one button away.
            return jsonify({
                "error": "That semester still has work in it. Archive it instead.",
                "counts": counts,
            }), 409
        if conn.execute("SELECT COUNT(*) n FROM semesters").fetchone()["n"] <= 1:
            conn.close()
            return jsonify({"error": "This is your only semester."}), 409
        conn.execute("DELETE FROM semesters WHERE id=?", (sid,))
        conn.commit()
        # If that was the active one, the stored pointer now names a row that is gone;
        # active_semester() falls through to the newest remaining term on the next read.
        conn.close()
        return jsonify({"ok": True})

    data = request.get_json(force=True) or {}
    name = (data.get("name") or row["name"] or "").strip() or default_term_name()
    status = data.get("status") or row["status"] or "active"
    if status not in ("active", "archived"):
        conn.close()
        return jsonify({"error": "A semester is either active or archived."}), 400
    archived_at = row["archived_at"] or ""
    if status == "archived" and (row["status"] or "active") != "archived":
        archived_at = datetime.utcnow().isoformat()
    if status == "active":
        archived_at = ""
    conn.execute(
        "UPDATE semesters SET name=?, start_date=?, end_date=?, status=?, archived_at=?"
        " WHERE id=?",
        (name,
         data.get("startDate", row["start_date"]) or "",
         data.get("endDate", row["end_date"]) or "",
         status, archived_at, sid))
    conn.commit()
    # Unarchiving clears any unlock, so the term goes back to being plainly editable
    # rather than editable-because-unlocked.
    if status == "active":
        session.pop(UNLOCK_KEY, None)
    out = serialize_semester(conn.execute(
        "SELECT * FROM semesters WHERE id=?", (sid,)).fetchone())
    conn.close()
    return jsonify(out)


@app.route("/api/history")
def history():
    """Every term's grade material, for the cumulative view.

    Deliberately lean: only what the grade maths needs, because this is the one place
    that loads several terms at once and a full /api/state per semester would carry
    every note and file along with it. The page computes the grades itself with the
    same functions it uses for the current term, so an archived GPA and a live one can
    never be worked out two different ways.
    """
    conn = get_db()
    out = []
    for sem in conn.execute(f"SELECT * FROM semesters ORDER BY {SEMESTER_ORDER}").fetchall():
        sid = sem["id"]
        classes = []
        for c in conn.execute(
                "SELECT * FROM classes WHERE semester_id=? ORDER BY created_at", (sid,)):
            classes.append({
                "id": c["id"], "code": c["code"], "name": c["name"], "color": c["color"],
                "gradeScale": json.loads(c["grade_scale"]) if c["grade_scale"] else None,
                "gradeCategories": [
                    {"id": g["id"], "name": g["name"] or "", "weight": g["weight"],
                     "dropLowest": g["drop_lowest"] or 0}
                    for g in conn.execute(
                        "SELECT * FROM grade_categories WHERE class_id=? ORDER BY sort_order",
                        (c["id"],))],
            })
        items = [{"classId": i["class_id"], "weight": i["weight"], "score": i["score"],
                  "categoryId": i["category_id"], "status": i["status"]}
                 for i in conn.execute(
                     "SELECT class_id, weight, score, category_id, status FROM items"
                     " WHERE semester_id=?", (sid,))]
        d = serialize_semester(sem)
        d["classes"] = classes
        d["items"] = items
        out.append(d)
    conn.close()
    return jsonify(out)


@app.route("/api/semesters/<sid>/unlock", methods=["POST"])
def unlock_semester(sid):
    """Let an archived term be edited for as long as this session lasts."""
    conn = get_db()
    row = conn.execute("SELECT * FROM semesters WHERE id=?", (sid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "No such semester."}), 404
    if (request.get_json(silent=True) or {}).get("lock"):
        session.pop(UNLOCK_KEY, None)
        return jsonify({"locked": True})
    session[UNLOCK_KEY] = sid
    return jsonify({"locked": False})


# ---------------- state ----------------

@app.route("/api/state")
def get_state():
    conn = get_db()
    # Everything below is the current semester's, and only its. Switching terms is a
    # server-side pointer rather than a query parameter, so a stale tab cannot ask for
    # a different one and nothing has to be threaded through 200 fetch calls.
    sem = active_semester(conn)
    sid = sem["id"]
    classes = conn.execute(
        "SELECT * FROM classes WHERE semester_id=? ORDER BY created_at", (sid,)).fetchall()
    items = conn.execute(
        "SELECT * FROM items WHERE semester_id=? ORDER BY created_at", (sid,)).fetchall()
    events = conn.execute(
        "SELECT * FROM events WHERE semester_id=? ORDER BY date, start", (sid,)).fetchall()
    unfiled_notes = conn.execute(
        "SELECT * FROM notes WHERE class_id IS NULL AND semester_id=? "
        "ORDER BY pinned DESC, updated_at DESC", (sid,)).fetchall()
    unfiled_materials = conn.execute(
        "SELECT * FROM materials WHERE class_id IS NULL AND semester_id=? "
        "ORDER BY created_at DESC", (sid,)).fetchall()
    # Every child row in one query per table. Without this the page costs one query per
    # class, per assignment and per file, which is what made saving feel slow.
    pre = state_children(
        conn,
        [c["id"] for c in classes],
        [i["id"] for i in items],
        [m["id"] for m in unfiled_materials],
    )
    result = {
        "semesters": [serialize_semester(r) for r in conn.execute(
            f"SELECT * FROM semesters ORDER BY {SEMESTER_ORDER}").fetchall()],
        "semester": serialize_semester(sem),
        # Whether there is anything to sync with, so the page can keep itself current
        # without asking a second endpoint on every load.
        "googleSync": bool(conn.execute(
            "SELECT 1 FROM calendar_accounts WHERE provider='google' AND enabled=1"
        ).fetchone()),
        # an archived term opens locked, so last year's grades cannot be edited by a
        # stray click; the unlock is per browser session and drops on switching away
        "locked": sem["status"] == "archived" and session.get(UNLOCK_KEY) != sid,
        "classes": [serialize_class(conn, c, pre) for c in classes],
        "items": [serialize_item(conn, i, pre) for i in items],
        "events": [serialize_event(e) for e in events],
        # kept under its old name so nothing in the page has to be renamed: the
        # current term is now simply the active semester
        "term": {
            "name": sem["name"] or "",
            "startDate": sem["start_date"] or "",
            "endDate": sem["end_date"] or "",
        },
        # syllabi that were read (and paid for) but not reviewed yet
        "pendingImports": [
            {"id": r["id"], "filename": r["filename"], "classId": r["class_id"], "createdAt": r["created_at"]}
            for r in conn.execute(
                "SELECT si.id, si.filename, si.class_id, si.created_at FROM syllabus_imports si"
                " LEFT JOIN classes c ON c.id = si.class_id"
                " WHERE si.status='review' AND (si.class_id IS NULL OR c.semester_id=?)"
                " ORDER BY si.created_at DESC", (sid,)).fetchall()
        ],
        # a timetable pulled from SFU but not applied yet, so closing the tab does not
        # lose the review the way it would if the draft only lived in the page
        "pendingCalendarImports": [
            {"id": r["id"], "label": r["label"], "source": r["source"],
             "classId": r["class_id"], "createdAt": r["created_at"]}
            for r in conn.execute(
                "SELECT ci.id, ci.label, ci.source, ci.class_id, ci.created_at FROM calendar_imports ci"
                " LEFT JOIN classes c ON c.id = ci.class_id"
                " WHERE ci.status='review' AND (ci.class_id IS NULL OR c.semester_id=?)"
                " ORDER BY ci.created_at DESC", (sid,)).fetchall()
        ],
        # Notes and files jotted down or dropped in before there was anywhere to put
        # them. They live outside every class until they are filed.
        "unfiled": {
            "notesList": [serialize_note(n, note_link_item_ids(pre, n["id"]))
                          for n in unfiled_notes],
            "materials": [serialize_material(m, material_item_ids(pre, m["id"], conn))
                          for m in unfiled_materials],
        },
    }
    conn.close()
    return jsonify(result)


@app.route("/api/term", methods=["PUT"])
def update_term():
    """Edit the current term's name and dates.

    Term settings used to be their own single-row table. They are now just the active
    semester, but the endpoint keeps its name and shape so the page's existing call
    site did not have to change.
    """
    data = request.get_json(force=True) or {}
    conn = get_db()
    sem = active_semester(conn)
    conn.execute(
        "UPDATE semesters SET name=?, start_date=?, end_date=? WHERE id=?",
        ((data.get("name", "") or "").strip() or sem["name"] or default_term_name(),
         data.get("startDate", ""), data.get("endDate", ""), sem["id"]),
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
        "INSERT INTO classes (id, semester_id, code, name, professor, color, notes, grade_scale, website, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            cid,
            active_semester_id(conn),
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
    ensure_default_file_folders(conn, cid)
    conn.commit()
    row = conn.execute("SELECT * FROM classes WHERE id=?", (cid,)).fetchone()
    out = serialize_class(conn, row) if row else {"id": cid}
    conn.close()
    return jsonify(out), 201


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
    # The saved class travels back for the same reason as an assignment does: the page
    # can redraw one class instead of refetching the term.
    row = conn.execute("SELECT * FROM classes WHERE id=?", (cid,)).fetchone()
    out = serialize_class(conn, row) if row else {"ok": True}
    conn.close()
    return jsonify(out)


@app.route("/api/search/files")
def search_file_contents():
    """Find a file by a phrase inside it, not just by its name.

    Every PDF and Word file has its text pulled out on upload and kept in
    `materials.extracted_text`, and until now nothing read it back: the Files page
    matched names, classes and folders, so a reading you remembered a sentence from
    was unfindable.

    A LIKE scan, deliberately. At a realistic library -- a few hundred files over a
    few terms -- it is a few milliseconds, and an index here would be a Postgres full
    text index that SQLite could not share, so the two databases would answer the same
    search differently. Worth revisiting at five semesters of readings, not before.
    """
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"query": q, "hits": []})
    conn = get_db()
    sid = active_semester_id(conn)
    like = "%" + q.lower().replace("%", r"\%").replace("_", r"\_") + "%"
    rows = conn.execute(
        "SELECT id, class_id, title, filename, extracted_text FROM materials"
        " WHERE semester_id=? AND extracted_text IS NOT NULL AND extracted_text != ''"
        " AND LOWER(extracted_text) LIKE ?"
        " ORDER BY created_at DESC LIMIT 40", (sid, like)).fetchall()
    hits = []
    for r in rows:
        text = r["extracted_text"] or ""
        at = text.lower().find(q.lower())
        start = max(0, at - 90)
        snippet = text[start:at + len(q) + 110].replace("\n", " ").strip()
        if start > 0:
            snippet = "\u2026" + snippet
        hits.append({"id": r["id"], "classId": r["class_id"],
                     "title": r["title"], "filename": r["filename"],
                     "snippet": " ".join(snippet.split()),
                     "count": text.lower().count(q.lower())})
    conn.close()
    return jsonify({"query": q, "hits": hits})


@app.route("/api/classes/<cid>/semester", methods=["PUT"])
def move_class_to_semester(cid):
    """Move a class, and everything hanging off it, to another term.

    A class created in the wrong term used to have to be deleted and made again, taking
    its files, notes and grades with it. Every child table that carries its own copy of
    `semester_id` is updated in the same transaction, because `semester_for` promises
    that copy matches the class's -- a half-moved class would show its assignments in
    one term and its files in another.

    Both terms have to be writable. Moving work out of an archived term is a write to
    that term, whatever the interface it is asked from.
    """
    data = request.get_json(force=True) or {}
    target = data.get("semesterId")
    conn = get_db()
    cls = conn.execute("SELECT * FROM classes WHERE id=?", (cid,)).fetchone()
    if not cls:
        conn.close()
        abort(404)
    sem = conn.execute("SELECT * FROM semesters WHERE id=?", (target,)).fetchone()
    if not sem:
        conn.close()
        return jsonify({"error": "No such term."}), 404
    for check, label in ((cls["semester_id"], "the term it is in now"),
                         (target, "the term you are moving it to")):
        row = conn.execute("SELECT status FROM semesters WHERE id=?", (check,)).fetchone()
        if row and row["status"] == "archived" and session.get(UNLOCK_KEY) != check:
            conn.close()
            return jsonify({"error": "That class cannot move while "
                                     + label + " is archived. Unlock it first."}), 423

    conn.execute("UPDATE classes SET semester_id=? WHERE id=?", (target, cid))
    for table in SEMESTER_SCOPED:
        conn.execute(f"UPDATE {table} SET semester_id=? WHERE class_id=?", (target, cid))
    conn.commit()
    row = conn.execute("SELECT * FROM classes WHERE id=?", (cid,)).fetchone()
    out = serialize_class(conn, row)
    conn.close()
    return jsonify(out)


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
        """INSERT INTO items (id, semester_id, class_id, title, type, due_date, due_time, status, completed_at, weight, score, notes, created_at,
                              category_id, location, import_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            iid,
            semester_for(conn, data.get("classId")),
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
    row = conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    out = serialize_item(conn, row) if row else {"id": iid}
    conn.close()
    return jsonify(out), 201


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
    # The saved row travels back, so the page can update the one assignment that
    # changed instead of reloading every class, file and note to see one new due date.
    row = conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    out = serialize_item(conn, row) if row else {"ok": True}
    conn.close()
    return jsonify(out)


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

# ---------------- Office previews ----------------

# Word, PowerPoint and Excel cannot be shown in a browser, so LibreOffice converts
# them to PDF once, on upload, and the PDF is what the preview pane loads. Converting
# on upload rather than on view means the cost is paid per file instead of per open,
# which matters because each conversion spawns a real LibreOffice process.
OFFICE_EXTS = {"doc", "docx", "odt", "rtf",
               "ppt", "pptx", "odp",
               "xls", "xlsx", "ods"}

# Where the binary lives: on Linux it is on PATH, on a Mac it is inside the app bundle.
SOFFICE_CANDIDATES = (
    "soffice",
    "libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


def soffice_path():
    """The LibreOffice binary, or None if this machine has not got it.

    Without it the app still works: Office files simply fall back to the download
    button they had before, which is why nothing here ever raises.
    """
    override = os.environ.get("SOFFICE_PATH")
    if override and os.path.exists(override):
        return override
    for candidate in SOFFICE_CANDIDATES:
        found = shutil.which(candidate) if not candidate.startswith("/") else (
            candidate if os.path.exists(candidate) else None)
        if found:
            return found
    return None


def office_ext(filename):
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return ext if ext in OFFICE_EXTS else None


def convert_to_pdf(src_path, out_dir, timeout=120):
    """Run LibreOffice once. Returns the produced PDF's path, or None.

    `-env:UserInstallation` gives each run its own profile directory. Without it two
    concurrent conversions fight over the same profile lock and the second one exits
    having produced nothing.
    """
    binary = soffice_path()
    if not binary or not os.path.exists(src_path):
        return None
    profile = os.path.join(out_dir, ".soffice-profile")
    try:
        subprocess.run(
            [binary, f"-env:UserInstallation=file://{profile}",
             "--headless", "--norestore", "--convert-to", "pdf",
             "--outdir", out_dir, src_path],
            check=True, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        return None
    produced = os.path.join(
        out_dir, os.path.splitext(os.path.basename(src_path))[0] + ".pdf")
    return produced if os.path.exists(produced) else None


def make_office_preview(mid, src_path, filename, user_id=None):
    """Convert one upload and record the result. Runs on a worker thread.

    `user_id` is not optional in practice on Postgres, and passing it is the whole
    point: a thread has no Flask request context, so `get_db()` cannot work out who
    the row belongs to. Without it the connection stays on the owning role, forced RLS
    grants it nothing, and every UPDATE below matches zero rows while reporting
    success. The visible symptom was `preview_status` stuck on 'pending' forever.
    """
    produced = convert_to_pdf(src_path, UPLOAD_DIR)
    conn = get_db(user_id=user_id)
    try:
        if produced:
            target = f"{mid}_preview.pdf"
            final = os.path.join(UPLOAD_DIR, target)
            if produced != final:
                os.replace(produced, final)
            conn.execute("UPDATE materials SET preview_name=?, preview_status='ready' WHERE id=?",
                         (target, mid))
            # The conversion is the only way a deck or a spreadsheet becomes readable.
            # Only fill an empty column: .docx already has a better reader of its own.
            row = conn.execute(
                "SELECT extracted_text FROM materials WHERE id=?", (mid,)).fetchone()
            if row and not (row["extracted_text"] or "").strip():
                text = extract_pdf_text(final)
                if text and text.strip():
                    conn.execute("UPDATE materials SET extracted_text=? WHERE id=?", (text, mid))
        else:
            conn.execute("UPDATE materials SET preview_status='failed' WHERE id=?", (mid,))
        conn.commit()
    finally:
        conn.close()


def queue_office_preview(mid, src_path, filename):
    """Start a conversion in the background if this file needs and can have one.

    The upload response must not wait on LibreOffice: a large deck takes seconds, and
    the student is already looking at the file list.
    """
    if not office_ext(filename) or not soffice_path():
        return
    # Read while the request context still exists; the thread will not have one.
    uid = current_user_id()
    conn = get_db()
    conn.execute("UPDATE materials SET preview_status='pending' WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    threading.Thread(target=make_office_preview, args=(mid, src_path, filename, uid),
                     daemon=True).start()


def backfill_office_text():
    """Read already-converted Office files that never had their text extracted.

    Separate from `backfill_extracted_text`, which runs at import before any of the
    Office helpers are defined, and cheap by comparison: the PDFs already exist, so
    this is pypdf only, no LibreOffice.

    Per account, for the same reason as `backfill_extracted_text`.
    """
    for_each_account(_backfill_office_text_for)


def _backfill_office_text_for(uid):
    conn = get_db(user_id=uid)
    try:
        rows = conn.execute(
            "SELECT id, preview_name FROM materials"
            " WHERE kind='file' AND preview_status='ready' AND preview_name IS NOT NULL"
            " AND (extracted_text IS NULL OR extracted_text='')").fetchall()
    except Exception:
        conn.close()
        return
    for r in rows:
        path = os.path.join(UPLOAD_DIR, r["preview_name"])
        if not os.path.exists(path):
            continue
        text = extract_pdf_text(path)
        if text and text.strip():
            conn.execute("UPDATE materials SET extracted_text=? WHERE id=?", (text, r["id"]))
    conn.commit()
    conn.close()


def backfill_office_previews():
    """Convert Office files uploaded before previews existed, one at a time.

    Deliberately serial and on one background thread: a library full of decks should
    not start twenty LibreOffice processes at boot.
    """
    if not soffice_path():
        return
    todo = []

    def collect(uid):
        conn = get_db(user_id=uid)
        try:
            rows = conn.execute(
                "SELECT id, stored_name, filename FROM materials"
                " WHERE kind='file' AND stored_name IS NOT NULL"
                " AND (preview_status IS NULL OR preview_status='')").fetchall()
        except Exception:
            return
        finally:
            conn.close()
        todo.extend((uid, r["id"], r["stored_name"], r["filename"]) for r in rows
                    if office_ext(r["filename"] or r["stored_name"]))

    for_each_account(collect)
    if not todo:
        return

    def run():
        for uid, mid, stored, filename in todo:
            path = os.path.join(UPLOAD_DIR, stored)
            if os.path.exists(path):
                make_office_preview(mid, path, filename, uid)
    threading.Thread(target=run, daemon=True).start()


backfill_office_previews()
backfill_office_text()


# ---------------- file folders ----------------

def serialize_file_folder(f):
    return {
        "id": f["id"],
        "classId": f["class_id"],
        "parentId": f["parent_id"],
        "name": f["name"] or "",
        "kind": f["kind"] or "custom",
        "sortOrder": f["sort_order"] or 0,
    }


@app.route("/api/classes/<cid>/file-folders", methods=["POST"])
def create_file_folder(cid):
    """A new folder, optionally inside another one."""
    data = request.get_json(force=True) or {}
    conn = get_db()
    if not conn.execute("SELECT 1 FROM classes WHERE id=?", (cid,)).fetchone():
        conn.close()
        abort(404)
    parent = data.get("parentId") or None
    if parent and not conn.execute(
            "SELECT 1 FROM file_folders WHERE id=? AND class_id=?", (parent, cid)).fetchone():
        conn.close()
        return jsonify({"error": "parent folder is not in this class"}), 400
    nxt = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM file_folders WHERE class_id=?"
        " AND parent_id IS ?", (cid, parent)).fetchone()["n"]
    fid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO file_folders (id, class_id, parent_id, name, kind, sort_order, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (fid, cid, parent, (data.get("name") or "New folder").strip() or "New folder",
         "custom", data.get("sortOrder", nxt), datetime.utcnow().isoformat()))
    conn.commit()
    row = conn.execute("SELECT * FROM file_folders WHERE id=?", (fid,)).fetchone()
    conn.close()
    return jsonify(serialize_file_folder(row)), 201


@app.route("/api/file-folders/<fid>", methods=["PUT"])
def update_file_folder(fid):
    """Rename a folder, or move it under a different parent."""
    data = request.get_json(force=True) or {}
    conn = get_db()
    row = conn.execute("SELECT * FROM file_folders WHERE id=?", (fid,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    fields, values = [], []
    if "name" in data:
        fields.append("name=?")
        values.append((data["name"] or "").strip() or row["name"] or "Folder")
    if "parentId" in data:
        parent = data["parentId"] or None
        # A folder cannot be dropped inside itself or inside its own child, which is
        # the one drag that would cut a branch loose from the tree entirely.
        if parent in folder_descendants(conn, fid):
            conn.close()
            return jsonify({"error": "a folder cannot be moved inside itself"}), 400
        if parent and not conn.execute(
                "SELECT 1 FROM file_folders WHERE id=? AND class_id=?", (parent, row["class_id"])).fetchone():
            conn.close()
            return jsonify({"error": "parent folder is not in this class"}), 400
        fields.append("parent_id=?")
        values.append(parent)
    if "sortOrder" in data:
        fields.append("sort_order=?")
        values.append(data["sortOrder"] or 0)
    if fields:
        values.append(fid)
        conn.execute(f"UPDATE file_folders SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    out = conn.execute("SELECT * FROM file_folders WHERE id=?", (fid,)).fetchone()
    conn.close()
    return jsonify(serialize_file_folder(out))


@app.route("/api/file-folders/<fid>", methods=["DELETE"])
def delete_file_folder(fid):
    """Delete a folder. The files in it are never deleted with it.

    Everything inside, files and subfolders alike, moves up to the deleted folder's
    own parent. Losing a folder should cost you an organising decision, not a file.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM file_folders WHERE id=?", (fid,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    branch = folder_descendants(conn, fid)
    marks = ",".join("?" * len(branch))
    conn.execute(f"UPDATE materials SET folder_id=? WHERE folder_id IN ({marks})",
                 [row["parent_id"]] + branch)
    conn.execute("UPDATE file_folders SET parent_id=? WHERE parent_id=?", (row["parent_id"], fid))
    conn.execute("DELETE FROM file_folders WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


def default_folder_for_upload(conn, class_id, category):
    """Where an uploaded file lands when nobody said.

    The filename rules already guess a category, so the guess picks the folder of the
    matching kind. A file the rules could not place stays at the top of the class
    rather than being buried in a folder the student did not choose.
    """
    if not class_id:
        return None
    kind = CATEGORY_TO_FOLDER_KIND.get(category or "")
    if not kind:
        return None
    return folder_id_for_kind(conn, class_id, kind, "Projects" if kind == "projects" else None)


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
    convert_after = None
    # a file can belong to one assignment as well as the class
    item_id = request.form.get("itemId") or (request.get_json(silent=True) or {}).get("itemId") or None

    # Where it is filed. An explicit folder wins; otherwise the filename guess picks
    # one. A folder belonging to another class is ignored rather than honoured.
    asked_folder = (request.form.get("folderId")
                    or (request.get_json(silent=True) or {}).get("folderId") or None)
    if asked_folder and not conn.execute(
            "SELECT 1 FROM file_folders WHERE id=? AND class_id IS ?", (asked_folder, cid)).fetchone():
        asked_folder = None

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
        folder = asked_folder or default_folder_for_upload(conn, cid, category)
        conn.execute(
            """INSERT INTO materials (id, semester_id, class_id, item_id, category, folder_id, title, kind, url, filename, stored_name, mimetype, size, extracted_text, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, semester_for(conn, cid), cid, None, category, folder, title, "file", None, original, stored, mimetype, size, text, now),
        )
        # Queued after this request's transaction is committed and closed, further
        # down. Starting it here takes a second connection to a database this one is
        # still writing to, and SQLite answers that with "database is locked".
        convert_after = (path, original)
    else:
        data = request.get_json(silent=True) or request.form
        url = (data.get("url") or "").strip()
        if not url:
            conn.close()
            return jsonify({"error": "url or file required"}), 400
        title = data.get("title") or url
        category = data.get("category", "other")
        folder = asked_folder or default_folder_for_upload(conn, cid, category)
        conn.execute(
            """INSERT INTO materials (id, semester_id, class_id, item_id, category, folder_id, title, kind, url, filename, stored_name, mimetype, size, extracted_text, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, semester_for(conn, cid), cid, None, category, folder, title, "link", url, None, None, None, None, None, now),
        )
    if item_id and conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
        conn.execute(
            "INSERT INTO item_files (id, item_id, material_id, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT DO NOTHING",
            (str(uuid.uuid4()), item_id, mid, now))
    conn.commit()
    conn.close()
    if convert_after:
        queue_office_preview(mid, convert_after[0], convert_after[1])
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
        # A folder belongs to one class, so moving between classes has to drop the
        # old folder or the file would claim to sit in a folder that cannot show it.
        # An explicit folderId in the same request is applied after this and wins.
        fields.append("folder_id=?")
        values.append(None)
    if "folderId" in data:
        folder = data["folderId"] or None
        if folder:
            fr = conn.execute("SELECT class_id FROM file_folders WHERE id=?", (folder,)).fetchone()
            cur = conn.execute("SELECT class_id FROM materials WHERE id=?", (mid,)).fetchone()
            target_class = data.get("classId", cur["class_id"] if cur else None) or None
            if not fr or fr["class_id"] != target_class:
                conn.close()
                return jsonify({"error": "that folder is not in this file's class"}), 400
        fields.append("folder_id=?")
        values.append(folder)
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
    m = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    conn.execute("DELETE FROM materials WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    if m and m["kind"] == "file":
        leftovers = [m["stored_name"]]
        if "preview_name" in m.keys():
            leftovers.append(m["preview_name"])
        for name in leftovers:
            if not name:
                continue
            path = os.path.join(UPLOAD_DIR, name)
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


@app.route("/api/materials/<mid>/preview")
def preview_material(mid):
    """The PDF LibreOffice made from an Office upload, rendered inline."""
    conn = get_db()
    m = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    conn.close()
    if not m or not ("preview_name" in m.keys()) or not m["preview_name"]:
        abort(404)
    if not os.path.exists(os.path.join(UPLOAD_DIR, m["preview_name"])):
        abort(404)
    return send_from_directory(UPLOAD_DIR, m["preview_name"], mimetype="application/pdf",
                               as_attachment=False)


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
        "INSERT INTO notes (id, semester_id, class_id, title, folder_id, text, linked_item_id, updated_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (nid, semester_for(conn, cid), cid, title, data.get("folderId"), text,
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
        "INSERT INTO notes (id, semester_id, class_id, title, folder_id, text, linked_item_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        # a copy stays in the term of the note it was copied from
        (new_id, row["semester_id"] or semester_for(conn, row["class_id"]), row["class_id"], ((row["title"] or "Untitled note") + " copy"),
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
        "INSERT INTO events (id, semester_id, class_id, title, kind, date, start, \"end\", all_day, location, notes, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            eid,
            semester_for(conn, data.get("classId")),
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
    # The export is of the term you are in. Exporting four years of deadlines into a
    # calendar at once would be worse than useless.
    term = active_semester(conn)
    sid = term["id"]
    classes = conn.execute("SELECT * FROM classes WHERE semester_id=?", (sid,)).fetchall()
    items_rows = conn.execute(
        "SELECT * FROM items WHERE semester_id=? AND due_date IS NOT NULL AND due_date != ''",
        (sid,)
    ).fetchall()
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


# ---------------------------------------------------------------------------
# Privacy and terms
#
# Google requires a homepage, a privacy policy and a terms link before an app using a
# sensitive scope can be published, and for the privacy policy to say plainly what
# happens to Google user data. These are served by the app itself so they live at a
# real address on the same domain, and they are deliberately outside /api so the gate
# lets a reviewer read them without an account.
# ---------------------------------------------------------------------------

LEGAL_CSS = """
  :root{color-scheme:light dark}
  body{margin:0;font:16px/1.65 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f4f5f7;color:#1c1d21}
  main{max-width:720px;margin:0 auto;padding:56px 22px 96px}
  h1{font-size:30px;margin:0 0 6px;letter-spacing:-.02em}
  h2{font-size:18px;margin:34px 0 8px}
  p,li{color:#3d3f47}
  .sub{color:#75777f;margin:0 0 28px}
  a{color:#3576d9}
  code{background:#e8e9ed;padding:1px 5px;border-radius:5px;font-size:14px}
  @media (prefers-color-scheme:dark){
    body{background:#17171d;color:#e9e9ee}
    p,li{color:#b9bac2} .sub{color:#8a8c96}
    code{background:#262730}
  }
"""


def legal_page(title, body_html):
    return Response(
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{title} — Vesta</title><style>{LEGAL_CSS}</style></head>"
        f"<body><main>{body_html}"
        f"<p style='margin-top:44px'><a href='/'>Back to Vesta</a></p>"
        f"</main></body></html>",
        mimetype="text/html")


@app.route("/privacy")
def privacy():
    return legal_page("Privacy", """
<h1>Privacy</h1>
<p class=sub>Vesta is a personal study planner, run by one student for himself and a
small number of friends. It is not a company and not a commercial service.</p>

<h2>What Vesta stores</h2>
<p>Only what you put into it: your classes and their schedules, assignments and due
dates, grades you record, notes you write, and files you upload. This is held in a
private database that only this application can reach, and every row is tied to your
account. Other people using this same installation cannot read your data; the database
enforces that, not just the application.</p>

<h2>Google Calendar</h2>
<p>If, and only if, you choose to connect Google Calendar, Vesta asks Google for
permission to read and write your calendar. It uses that access for one purpose: to
keep a calendar named <code>Vesta</code> in step with your assignment and exam due
dates, and to read events back so the two do not contradict each other.</p>
<p>Vesta's use of information received from Google APIs follows the
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API
Services User Data Policy</a>, including its Limited Use requirements. Calendar data is
never sold, never used for advertising, never used to train any model, and never shared
with anyone else. It is not read by a human. You can disconnect at any time from
Settings, or revoke access from your Google account, and Vesta stops immediately.</p>

<h2>Artificial intelligence features</h2>
<p>Some features (drafting, study outlines, quizzes, and reading a syllabus) send the
relevant assignment text or uploaded document to Anthropic's API to generate a response.
Those features only run when you press the button that starts them. Nothing is sent
anywhere for the app's ordinary use.</p>

<h2>What Vesta does not do</h2>
<p>No advertising, no analytics, no tracking, no selling or sharing of data with anyone.
Your email address is used to identify your account and nothing else.</p>

<h2>Deleting your data</h2>
<p>Delete individual items in the app at any time. To remove your account and everything
attached to it, ask the developer and it will be deleted from the database outright.</p>

<h2>Contact</h2>
<p>Saif Abuhaltam, the developer, at the support email listed on the Google consent
screen for this app.</p>
""")


@app.route("/terms")
def terms():
    return legal_page("Terms", """
<h1>Terms of use</h1>
<p class=sub>Short, because this is a study planner run by a student for a handful of
people.</p>

<h2>What this is</h2>
<p>A private tool for organising coursework. Access is by invitation: an account can
only be created with an email the developer has added.</p>

<h2>What you can expect</h2>
<p>Vesta is provided as is, with no guarantee of availability and no warranty. It runs
on modest hosting and can be down, lose a deployment, or change without notice. Keep
your own copies of anything you cannot afford to lose. The developer is not liable for
lost work or missed deadlines.</p>

<h2>What is expected of you</h2>
<p>Use it for your own coursework. Do not upload anything you do not have the right to
store. The AI features produce drafts and study material to work from, not work to hand
in as your own; what you submit to your institution, and whether it complies with that
institution's academic integrity rules, is your responsibility.</p>

<h2>Ending it</h2>
<p>Stop using it whenever you like and ask for your data to be deleted. The developer
may withdraw access, particularly if costs or misuse make that necessary.</p>
""")


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
    # Which schema migrations this database has actually taken. A deploy can look
    # healthy while a migration silently did not run, and every symptom of that is a
    # confusing query error somewhere else entirely.
    if _db.DATABASE_URL:
        try:
            import psycopg
            with psycopg.connect(_db.DATABASE_URL) as c:
                with c.cursor() as cur:
                    cur.execute("select id from schema_migrations order by applied_at")
                    out["migrations"] = [r[0] for r in cur.fetchall()]
        except Exception as e:
            out["migrations"] = f"unreadable: {e.__class__.__name__}"
    else:
        out["migrations"] = "sqlite: migrations run in-process at startup"
    # Google's OAuth settings, so a mismatch can be seen rather than inferred from an
    # error page. Neither value is secret: the client id and the callback URL both
    # travel in the address bar during any sign-in, which is precisely why they have to
    # match what is registered. The client *secret* is never reported.
    # Whether the sign-in screen's "Invite only" is telling the truth.
    out["inviteOnly"] = bool(vesta_auth.allowlist())
    out["invitedCount"] = len(vesta_auth.allowlist())
    # The ceiling on AI spend across every account, which is the only number the people
    # spending cannot raise themselves. null means there is none.
    try:
        import ai as _ai
        out["aiGlobalDailyCapUsd"] = _ai.GLOBAL_CAP_USD
    except Exception:
        out["aiGlobalDailyCapUsd"] = None
    try:
        import calendar_api as _cal
        out["google"] = {
            "configured": bool(os.environ.get("GOOGLE_CLIENT_ID")
                               and os.environ.get("GOOGLE_CLIENT_SECRET")),
            "clientId": os.environ.get("GOOGLE_CLIENT_ID") or None,
            "callbackUrl": _cal.redirect_uri(),
        }
    except Exception as e:
        out["google"] = {"error": str(e)[:120]}
    if _db.DATABASE_URL:
        try:
            conn = _db.get_db(user_id=None)
            conn.as_owner()
            row = conn.execute(
                "select count(*) as n from pg_class c join pg_namespace ns"
                " on ns.oid = c.relnamespace where ns.nspname='public' and c.relkind='r'"
            ).fetchone()
            out["tables"] = row["n"] if row else 0
            # Whether the isolation between accounts is real on THIS database.
            #
            # The role that matters is `authenticated`, not the one Vesta connects as.
            # Every request calls `set role authenticated` before it touches a table,
            # and Postgres evaluates row level security against the *current* role, so
            # a superuser connection that has dropped to `authenticated` is still held
            # to the policies. Measuring the connecting role instead reports a scary
            # false negative on any host that hands out a superuser, which Railway does.
            #
            # The corollary is worth knowing: on such a host a query that never calls
            # `become()` runs as that superuser and reads *every* account's rows. It
            # fails open, not closed. `db.get_db()` outside a request context is the
            # way that happens.
            role = conn.execute(
                "select rolsuper or rolbypassrls as bypasses from pg_roles"
                " where rolname = 'authenticated'").fetchone()
            out["rlsEnforced"] = (not role["bypasses"]) if role else False
            out["connectsAsSuperuser"] = bool(conn.execute(
                "select rolsuper or rolbypassrls as su from pg_roles"
                " where rolname = current_user").fetchone()["su"])
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

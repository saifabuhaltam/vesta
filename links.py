"""How the things in Vesta connect: files to assignments, what a search finds inside
them, and which links are worth suggesting.

A file has one home (a class, or the Inbox) but can be attached to any number of
assignments and referenced by any number of notes, so nothing is uploaded twice.
Nothing here links anything on its own: suggestions come back for the student to
accept with one click.
"""
import re
import uuid
from datetime import datetime

from flask import Blueprint, abort, jsonify, request

from ai import strip_html
from db import get_db, active_semester_id

bp = Blueprint("links", __name__)

MIN_TITLE = 4          # shorter titles ("Lab", "Q1") match too much to be worth suggesting
SNIPPET = 70


def now():
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Attaching files to assignments
# ---------------------------------------------------------------------------
@bp.route("/api/items/<iid>/files", methods=["POST"])
def attach_file(iid):
    mid = (request.get_json(force=True) or {}).get("materialId")
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM items WHERE id=?", (iid,)).fetchone():
            abort(404)
        if not mid or not conn.execute("SELECT 1 FROM materials WHERE id=?", (mid,)).fetchone():
            return jsonify({"error": "Pick a file to attach."}), 400
        conn.execute(
            "INSERT INTO item_files (id, item_id, material_id, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT DO NOTHING",
            (str(uuid.uuid4()), iid, mid, now()))
        conn.commit()
        return jsonify({"ok": True}), 201
    finally:
        conn.close()


@bp.route("/api/items/<iid>/files/<mid>", methods=["DELETE"])
def detach_file(iid, mid):
    """Take a file off an assignment. The file itself stays where it lives."""
    conn = get_db()
    conn.execute("DELETE FROM item_files WHERE item_id=? AND material_id=?", (iid, mid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


def item_ids_for_file(conn, mid):
    return [r["item_id"] for r in conn.execute(
        "SELECT item_id FROM item_files WHERE material_id=? ORDER BY created_at", (mid,))]


# ---------------------------------------------------------------------------
# What connects to what
# ---------------------------------------------------------------------------
def related_for_file(conn, mid):
    out = [{"type": "item", "id": r["id"], "title": r["title"] or "Untitled", "classId": r["class_id"]}
           for r in conn.execute(
               "SELECT i.id, i.title, i.class_id FROM item_files f JOIN items i ON i.id=f.item_id "
               "WHERE f.material_id=?", (mid,))]
    out += [{"type": "note", "id": r["id"], "title": r["title"] or "Untitled note", "classId": r["class_id"]}
            for r in conn.execute(
                "SELECT DISTINCT n.id, n.title, n.class_id FROM note_links l JOIN notes n ON n.id=l.note_id "
                "WHERE l.file_id=? AND (n.deleted_at IS NULL OR n.deleted_at='')", (mid,))]
    return out


def related_for_note(conn, n):
    out = []
    ids = set()
    if n["linked_item_id"]:
        ids.add(n["linked_item_id"])
    ids |= {r["item_id"] for r in conn.execute(
        "SELECT item_id FROM note_links WHERE note_id=? AND item_id IS NOT NULL", (n["id"],))}
    for iid in ids:
        it = conn.execute("SELECT id, title, class_id FROM items WHERE id=?", (iid,)).fetchone()
        if it:
            out.append({"type": "item", "id": it["id"], "title": it["title"] or "Untitled", "classId": it["class_id"]})
    out += [{"type": "file", "id": r["id"], "title": r["title"] or r["filename"] or "File", "classId": r["class_id"]}
            for r in conn.execute(
                "SELECT m.id, m.title, m.filename, m.class_id FROM note_links l JOIN materials m ON m.id=l.file_id "
                "WHERE l.note_id=?", (n["id"],))]
    return out


def related_for_item(conn, iid):
    out = [{"type": "file", "id": r["id"], "title": r["title"] or r["filename"] or "File", "classId": r["class_id"]}
           for r in conn.execute(
               "SELECT m.id, m.title, m.filename, m.class_id FROM item_files f JOIN materials m ON m.id=f.material_id "
               "WHERE f.item_id=?", (iid,))]
    out += [{"type": "note", "id": r["id"], "title": r["title"] or "Untitled note", "classId": r["class_id"]}
            for r in conn.execute(
                "SELECT id, title, class_id FROM notes WHERE (deleted_at IS NULL OR deleted_at='') AND "
                "(linked_item_id=? OR id IN (SELECT note_id FROM note_links WHERE item_id=?))", (iid, iid))]
    return out


def snippet(text, q):
    plain = re.sub(r"\s+", " ", text or "").strip()
    at = plain.lower().find(q.lower())
    if at < 0:
        return plain[:SNIPPET * 2]
    start = max(0, at - SNIPPET)
    end = min(len(plain), at + len(q) + SNIPPET)
    return ("…" if start else "") + plain[start:end] + ("…" if end < len(plain) else "")


# ---------------------------------------------------------------------------
# Search, including inside files and notes
#
# Extracted file text never goes to the browser in /api/state (it would make every
# page load carry every PDF), so searching contents happens here.
# ---------------------------------------------------------------------------
@bp.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    like = f"%{q}%"
    ql = q.lower()
    conn = get_db()
    codes = {r["id"]: (r["code"] or r["name"] or "") for r in conn.execute("SELECT id, code, name FROM classes")}
    # Search deliberately spans every term, archived ones included: a finished course
    # is still the place last year's essay lives. Each result carries the term it came
    # from so a hit from two years ago is never mistaken for this week's work.
    here = active_semester_id(conn)
    terms = {r["id"]: (r["name"] or "") for r in conn.execute("SELECT id, name FROM semesters")}

    def term_of(row):
        sid = row["semester_id"] if "semester_id" in row.keys() else None
        return {"semesterId": sid or "",
                "semesterName": terms.get(sid, "") if sid and sid != here else "",
                "pastTerm": bool(sid and sid != here)}

    results = []

    for m in conn.execute(
            "SELECT id, semester_id, class_id, title, filename, extracted_text FROM materials "
            "WHERE title LIKE ? OR filename LIKE ? OR extracted_text LIKE ? ORDER BY created_at DESC LIMIT 25",
            (like, like, like)):
        name = m["title"] or m["filename"] or "File"
        in_name = ql in (name + " " + (m["filename"] or "")).lower()
        results.append({
            "type": "file", "id": m["id"], "title": name, "classId": m["class_id"],
            "classCode": codes.get(m["class_id"], "Inbox"), **term_of(m),
            "matchedIn": "name" if in_name else "contents",
            "snippet": "" if in_name else snippet(m["extracted_text"], q),
            "related": related_for_file(conn, m["id"]),
        })

    for n in conn.execute(
            "SELECT * FROM notes WHERE (deleted_at IS NULL OR deleted_at='') "
            "AND (title LIKE ? OR text LIKE ?) ORDER BY updated_at DESC LIMIT 40", (like, like)):
        plain = strip_html(n["text"] or "")
        in_title = ql in (n["title"] or "").lower()
        if not in_title and ql not in plain.lower():
            continue                      # the match was inside the markup, not the words
        results.append({
            "type": "note", "id": n["id"], "title": n["title"] or plain[:60] or "Untitled note",
            "classId": n["class_id"], "classCode": codes.get(n["class_id"], "Inbox"), **term_of(n),
            "matchedIn": "title" if in_title else "contents",
            "snippet": "" if in_title else snippet(plain, q),
            "related": related_for_note(conn, n),
        })

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    fields = [c for c in ("title", "description", "notes") if c in cols]
    where = " OR ".join(f"{c} LIKE ?" for c in fields)
    for it in conn.execute(f"SELECT * FROM items WHERE {where} ORDER BY due_date LIMIT 25",
                           tuple(like for _ in fields)):
        in_title = ql in (it["title"] or "").lower()
        body = " ".join((it[c] or "") for c in fields if c != "title")
        results.append({
            "type": "item", "id": it["id"], "title": it["title"] or "Untitled", "classId": it["class_id"],
            "classCode": codes.get(it["class_id"], "General"), **term_of(it),
            "matchedIn": "title" if in_title else "details",
            "snippet": "" if in_title else snippet(strip_html(body), q),
            "related": related_for_item(conn, it["id"]),
        })

    conn.close()
    return jsonify({"results": results})


# ---------------------------------------------------------------------------
# Suggested links
# ---------------------------------------------------------------------------
def mentions(text_lower, title):
    t = (title or "").strip().lower()
    if len(t) < MIN_TITLE:
        return False
    return re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", text_lower) is not None


def stem(filename):
    return re.sub(r"\.[a-z0-9]{1,5}$", "", filename or "", flags=re.I).replace("_", " ").replace("-", " ")


@bp.route("/api/notes/<nid>/suggestions")
def note_suggestions(nid):
    conn = get_db()
    n = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    if not n:
        conn.close()
        abort(404)
    text = ((n["title"] or "") + " " + strip_html(n["text"] or "")).lower()
    have_items = {n["linked_item_id"]} | {r["item_id"] for r in conn.execute(
        "SELECT item_id FROM note_links WHERE note_id=? AND item_id IS NOT NULL", (nid,))}
    have_files = {r["file_id"] for r in conn.execute(
        "SELECT file_id FROM note_links WHERE note_id=? AND file_id IS NOT NULL", (nid,))}
    out = []
    for it in conn.execute(
            "SELECT id, title, class_id FROM items"
            " ORDER BY CASE WHEN class_id = ? THEN 0 ELSE 1 END, due_date", (n["class_id"],)):
        if it["id"] not in have_items and mentions(text, it["title"]):
            out.append({"kind": "item", "id": it["id"], "title": it["title"],
                        "reason": f'mentions "{it["title"]}"'})
    for m in conn.execute(
            "SELECT id, title, filename, class_id FROM materials"
            " ORDER BY CASE WHEN class_id = ? THEN 0 ELSE 1 END, created_at DESC",
            (n["class_id"],)):
        if m["id"] in have_files:
            continue
        for label in (m["title"], stem(m["filename"])):
            if mentions(text, label):
                out.append({"kind": "file", "id": m["id"], "title": m["title"] or m["filename"],
                            "reason": f'mentions "{label}"'})
                break
    conn.close()
    return jsonify(out[:6])


@bp.route("/api/materials/<mid>/suggestions")
def file_suggestions(mid):
    conn = get_db()
    m = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    if not m:
        conn.close()
        abort(404)
    text = " ".join([m["title"] or "", stem(m["filename"]), m["extracted_text"] or ""]).lower()
    attached = set(item_ids_for_file(conn, mid))
    out = []
    for it in conn.execute(
            "SELECT id, title, class_id FROM items"
            " ORDER BY CASE WHEN class_id = ? THEN 0 ELSE 1 END, due_date", (m["class_id"],)):
        if it["id"] not in attached and mentions(text, it["title"]):
            out.append({"kind": "item", "id": it["id"], "title": it["title"],
                        "reason": f'mentions "{it["title"]}"'})
    conn.close()
    return jsonify(out[:6])

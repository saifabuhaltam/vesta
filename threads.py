"""Continuing conversations about a class.

A thread is the answer to "I attached four readings to draft discussion 1, and now
discussion 2 wants the same four". Its sources are pinned once and reused on every
turn, and its history is kept, so the next question can build on the last answer
instead of starting from nothing.

Threads sit beside the one-shot Headstart tools rather than replacing them: the tools
are still the fastest way to start, because they mean you do not have to know what to
ask. Starting a thread from a tool posts that tool's instruction as the first message.
"""
import json
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from db import get_db, active_semester_id
from ai import (AiRefused, TOOLS, call_claude_chat, collect_sources, settings,
                spent_today, strip_html)

bp = Blueprint("threads", __name__)

# A thread's own turns are short next to the material, so the ceiling can be generous
# without costing much: the expensive half of the prompt is a cache read after turn one.
CHAT_MAX_TOKENS = 4000

# How much of the conversation travels with each turn. Kept well inside the context
# window; a thread longer than this keeps its oldest turns on screen but stops sending
# them, which is cheaper than it sounds because the material is what dominates the bill.
HISTORY_TURNS = 40


def now_iso():
    return datetime.utcnow().isoformat()


def serialize_thread(r, message_count=None, last_at=None):
    return {
        "id": r["id"],
        "classId": r["class_id"],
        "itemId": r["item_id"],
        "title": r["title"] or "Untitled thread",
        "archived": bool(r["archived"]),
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
        "messageCount": message_count,
        "lastMessageAt": last_at,
    }


def serialize_message(r):
    return {
        "id": r["id"],
        "role": r["role"],
        "content": r["content"],
        "tool": r["tool"],
        "createdAt": r["created_at"],
        "usage": {
            "inputTokens": r["input_tokens"] or 0,
            "outputTokens": r["output_tokens"] or 0,
            "cacheReadTokens": r["cache_read_tokens"] or 0,
            "cacheWriteTokens": r["cache_write_tokens"] or 0,
        },
    }


def thread_selection(conn, tid):
    """The pinned sources, in the shape `collect_sources` already understands."""
    rows = conn.execute("SELECT * FROM thread_sources WHERE thread_id=?", (tid,)).fetchall()
    sel = {"materialIds": [], "noteIds": [], "folderIds": [], "syllabusIds": []}
    for r in rows:
        if r["material_id"]:
            sel["materialIds"].append(r["material_id"])
        elif r["note_id"]:
            sel["noteIds"].append(r["note_id"])
        elif r["folder_id"]:
            sel["folderIds"].append(r["folder_id"])
        elif r["syllabus_id"]:
            sel["syllabusIds"].append(r["syllabus_id"])
    return sel


def write_selection(conn, tid, selection):
    """Replace a thread's pinned sources. Called on create and on every edit."""
    conn.execute("DELETE FROM thread_sources WHERE thread_id=?", (tid,))
    selection = selection or {}
    for key, col in (("materialIds", "material_id"), ("noteIds", "note_id"),
                     ("folderIds", "folder_id"), ("syllabusIds", "syllabus_id")):
        for ref in dict.fromkeys(selection.get(key) or []):
            conn.execute(
                f"INSERT INTO thread_sources (id, thread_id, {col}, created_at) VALUES (?,?,?,?)",
                (str(uuid.uuid4()), tid, ref, now_iso()))


def pinned_list(conn, tid, selection):
    """Every pinned source by name, and whether there is any text in it to read.

    `collect_sources` returns only what it could actually read, so a pinned PDF that
    has no extractable text simply vanishes from it. Showing that list alone would
    mean ticking three things and seeing one, with nothing saying why.
    """
    out = []
    for mid in selection.get("materialIds") or []:
        r = conn.execute("SELECT id, title, filename, extracted_text FROM materials WHERE id=?",
                         (mid,)).fetchone()
        if r:
            out.append({"type": "file", "id": r["id"],
                        "title": r["title"] or r["filename"] or "File",
                        "readable": bool((r["extracted_text"] or "").strip())})
    for nid in selection.get("noteIds") or []:
        r = conn.execute("SELECT id, title, text FROM notes WHERE id=?", (nid,)).fetchone()
        if r:
            out.append({"type": "note", "id": r["id"], "title": r["title"] or "Untitled note",
                        "readable": bool(strip_html(r["text"] or "").strip())})
    for fid in selection.get("folderIds") or []:
        r = conn.execute("SELECT id, name FROM note_folders WHERE id=?", (fid,)).fetchone()
        if r:
            out.append({"type": "folder", "id": r["id"], "title": r["name"] or "Folder",
                        "readable": True})
    for sid in selection.get("syllabusIds") or []:
        r = conn.execute("SELECT id, title FROM syllabus_topics WHERE id=?", (sid,)).fetchone()
        if r:
            out.append({"type": "topic", "id": r["id"], "title": r["title"] or "Topic",
                        "readable": True})
    return out


def thread_counts(conn, tid):
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS last FROM thread_messages WHERE thread_id=?",
        (tid,)).fetchone()
    return (row["n"] if row else 0), (row["last"] if row else None)


def load_thread(conn, tid):
    return conn.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()


# ---------------------------------------------------------------------------
# The threads themselves
# ---------------------------------------------------------------------------
@bp.route("/api/threads", methods=["GET", "POST"])
def threads_collection():
    conn = get_db()
    try:
        if request.method == "GET":
            cid = request.args.get("classId")
            sql = ("SELECT t.*, (SELECT COUNT(*) FROM thread_messages m WHERE m.thread_id=t.id) AS n,"
                   " (SELECT MAX(created_at) FROM thread_messages m WHERE m.thread_id=t.id) AS last"
                   " FROM threads t WHERE t.archived=0")
            args = []
            if cid:
                sql += " AND t.class_id=?"
                args.append(cid)
            sql += " ORDER BY COALESCE(t.updated_at, t.created_at) DESC"
            rows = conn.execute(sql, tuple(args)).fetchall()
            return jsonify([serialize_thread(r, r["n"], r["last"]) for r in rows])

        data = request.get_json(force=True) or {}
        class_id = data.get("classId")
        if not class_id:
            return jsonify({"error": "A thread belongs to a class."}), 400
        tid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO threads (id, semester_id, class_id, item_id, title, archived,"
            " created_at, updated_at) VALUES (?,?,?,?,?,0,?,?)",
            (tid, active_semester_id(conn), class_id, data.get("itemId"),
             (data.get("title") or "New thread").strip()[:120], now_iso(), now_iso()))
        write_selection(conn, tid, data.get("selection"))
        conn.commit()
        return jsonify(serialize_thread(load_thread(conn, tid), 0, None)), 201
    finally:
        conn.close()


@bp.route("/api/threads/<tid>", methods=["GET", "PUT", "DELETE"])
def one_thread(tid):
    conn = get_db()
    try:
        row = load_thread(conn, tid)
        if not row:
            return jsonify({"error": "No such thread."}), 404

        if request.method == "DELETE":
            conn.execute("DELETE FROM threads WHERE id=?", (tid,))
            conn.commit()
            return jsonify({"ok": True})

        if request.method == "PUT":
            data = request.get_json(force=True) or {}
            if "title" in data:
                conn.execute("UPDATE threads SET title=?, updated_at=? WHERE id=?",
                             ((data.get("title") or "Untitled thread").strip()[:120], now_iso(), tid))
            if "archived" in data:
                conn.execute("UPDATE threads SET archived=?, updated_at=? WHERE id=?",
                             (1 if data.get("archived") else 0, now_iso(), tid))
            if "selection" in data:
                write_selection(conn, tid, data.get("selection"))
                conn.execute("UPDATE threads SET updated_at=? WHERE id=?", (now_iso(), tid))
            conn.commit()
            row = load_thread(conn, tid)

        messages = conn.execute(
            "SELECT * FROM thread_messages WHERE thread_id=? ORDER BY created_at", (tid,)).fetchall()
        selection = thread_selection(conn, tid)
        # What the pinned sources actually amount to, so the thread can say what it reads
        # without the caller having to work it out from four lists of ids.
        _, used = collect_sources(conn, selection, row["class_id"], row["item_id"])
        n, last = thread_counts(conn, tid)
        out = serialize_thread(row, n, last)
        out["messages"] = [serialize_message(m) for m in messages]
        out["selection"] = selection
        out["sources"] = used
        # everything pinned, readable or not, so the thread can say which is which
        out["pinned"] = pinned_list(conn, tid, selection)
        return jsonify(out)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Saying something in a thread
# ---------------------------------------------------------------------------
@bp.route("/api/threads/<tid>/messages", methods=["POST"])
def send_message(tid):
    """Add a turn and answer it.

    The user's message is written before the model is called, so a failed or refused
    call does not lose what they typed: it stays in the thread and can be retried.
    """
    data = request.get_json(force=True) or {}
    tool_key = data.get("tool")
    text = (data.get("text") or "").strip()

    # A tool press is just a first message with words already in it.
    if tool_key and tool_key in TOOLS and not text:
        text = TOOLS[tool_key]["prompt"]
    if not text:
        return jsonify({"error": "Say something first."}), 400

    conn = get_db()
    try:
        row = load_thread(conn, tid)
        if not row:
            return jsonify({"error": "No such thread."}), 404

        selection = thread_selection(conn, tid)
        context, used = collect_sources(conn, selection, row["class_id"], row["item_id"])

        prior = conn.execute(
            "SELECT role, content FROM thread_messages WHERE thread_id=? ORDER BY created_at",
            (tid,)).fetchall()
        history = [{"role": m["role"], "content": m["content"]} for m in prior][-HISTORY_TURNS:]
        history.append({"role": "user", "content": text})

        user_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO thread_messages (id, thread_id, role, content, tool, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, tid, "user", text, tool_key if tool_key in TOOLS else None, now_iso()))
        conn.commit()

        try:
            result = call_claude_chat(conn, "thread", context, history,
                                      CHAT_MAX_TOKENS, bool(data.get("confirmed")))
        except AiRefused as e:
            # The turn stays in the thread; the caller is told why nothing came back.
            payload = dict(e.payload)
            payload["userMessageId"] = user_id
            return jsonify(payload), e.status

        reply_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO thread_messages (id, thread_id, role, content, tool, input_tokens,"
            " output_tokens, cache_read_tokens, cache_write_tokens, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (reply_id, tid, "assistant", result["text"], None, result["inputTokens"],
             result["outputTokens"], result["cacheReadTokens"], result["cacheWriteTokens"],
             now_iso()))
        conn.execute("UPDATE threads SET updated_at=? WHERE id=?", (now_iso(), tid))
        conn.commit()

        reply = conn.execute("SELECT * FROM thread_messages WHERE id=?", (reply_id,)).fetchone()
        user_row = conn.execute("SELECT * FROM thread_messages WHERE id=?", (user_id,)).fetchone()
        return jsonify({
            "userMessage": serialize_message(user_row),
            "reply": serialize_message(reply),
            "sources": used,
            "usage": spent_today(conn, settings(conn)),
        })
    finally:
        conn.close()


@bp.route("/api/threads/<tid>/messages/<mid>", methods=["DELETE"])
def delete_message(tid, mid):
    """Drop one turn, so a bad question does not have to stay in the history forever."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM thread_messages WHERE id=? AND thread_id=?", (mid, tid))
        conn.execute("UPDATE threads SET updated_at=? WHERE id=?", (now_iso(), tid))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()

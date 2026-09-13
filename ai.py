"""Vesta's AI layer: Headstart tools, Quiz Me, flashcards and practice tests.

Three ideas hold this together.

**Context is explicit.** A generation is only as good as what it was given, so every
call records exactly which files, notes, folders and syllabus topics it read. You
can pick them yourself, or let Vesta choose what looks relevant to the assignment.
Either way the run stores its sources, so a bad answer is debuggable and a good one
is repeatable.

**Cost is visible before it is spent.** Every prompt is measured before it is sent.
Anything large asks for confirmation, and a daily ceiling stops the day getting away
from you. Usage is recorded per call, so the number in the interface is real rather
than an estimate.

**Nothing here writes to the note or the assignment on its own.** Output comes back
for the student to accept, edit or throw away.
"""
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta

from flask import Blueprint, abort, jsonify, request

import anthropic

from db import get_db, active_semester_id, semester_for

bp = Blueprint("ai", __name__)

# ---------------------------------------------------------------------------
# Cost
#
# Rates are per million tokens and are stored in settings so they can be corrected
# without a code change. These defaults are a starting point, not gospel: check
# anthropic.com/pricing and update them from the app if they drift.
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "model": "claude-sonnet-5",
    "input_per_m": 2.0,
    "output_per_m": 10.0,
    "daily_cap_usd": 1.00,
    "confirm_over_usd": 0.05,
}

# Rough but stable: English prose runs about four characters per token.
CHARS_PER_TOKEN = 4


def settings(conn):
    row = conn.execute("SELECT value FROM app_settings WHERE key='ai'").fetchone()
    out = dict(DEFAULT_SETTINGS)
    if row and row["value"]:
        try:
            out.update(json.loads(row["value"]))
        except Exception:
            pass
    return out


def save_settings(conn, patch):
    current = settings(conn)
    current.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
    # Not ON CONFLICT: app_settings is keyed (user_id, key) on Postgres, so a
    # conflict target of `key` alone matches no constraint. Row level security scopes
    # the UPDATE to this account, so the insert only fires for a genuinely new one.
    blob = json.dumps(current)
    if not conn.execute("UPDATE app_settings SET value=? WHERE key='ai'", (blob,)).rowcount:
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('ai', ?)", (blob,))
    conn.commit()
    return current


def estimate_tokens(text):
    return max(1, len(text or "") // CHARS_PER_TOKEN)


# Dollars per million tokens (input, output). A call is priced by the model that
# actually ran it, so an Opus syllabus read is not counted at Sonnet's rate. The
# configured model's price comes from settings, so it can be corrected in the app.
MODEL_PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def price_for(cfg, model=None):
    model = model or cfg["model"]
    if model == cfg["model"]:
        return cfg["input_per_m"], cfg["output_per_m"]
    return MODEL_PRICES.get(model, (cfg["input_per_m"], cfg["output_per_m"]))


def estimate_cost(cfg, in_tokens, out_tokens, model=None):
    per_in, per_out = price_for(cfg, model)
    return (in_tokens / 1_000_000) * per_in + (out_tokens / 1_000_000) * per_out


def spent_today(conn, cfg):
    day = datetime.utcnow().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT model, COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o, "
        "COUNT(*) AS n FROM ai_usage WHERE day=? GROUP BY model",
        (day,),
    ).fetchall()
    return {
        "input_tokens": sum(r["i"] for r in rows),
        "output_tokens": sum(r["o"] for r in rows),
        "calls": sum(r["n"] for r in rows),
        "usd": round(sum(estimate_cost(cfg, r["i"], r["o"], r["model"]) for r in rows), 4),
    }


def record_usage(conn, kind, model, in_tokens, out_tokens):
    conn.execute(
        "INSERT INTO ai_usage (id, kind, model, input_tokens, output_tokens, day, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), kind, model, in_tokens, out_tokens,
         datetime.utcnow().strftime("%Y-%m-%d"), datetime.utcnow().isoformat()),
    )
    conn.commit()


class AiRefused(Exception):
    """Raised when a call is blocked before it costs anything."""

    def __init__(self, payload, status=402):
        self.payload = payload
        self.status = status


def call_claude(conn, kind, prompt, max_tokens=4000, confirmed=False):
    """Send one prompt, after checking it is affordable.

    Refuses before spending in two cases: the daily ceiling is already reached, or
    this particular call is large enough to be worth a second look. The second
    refusal carries the estimate so the interface can show it and ask.
    """
    cfg = settings(conn)
    in_tokens = estimate_tokens(prompt)
    est = estimate_cost(cfg, in_tokens, max_tokens)
    used = spent_today(conn, cfg)

    if cfg["daily_cap_usd"] and used["usd"] >= cfg["daily_cap_usd"]:
        raise AiRefused({
            "error": "You have reached today's AI limit.",
            "reason": "daily_cap",
            "spentToday": used["usd"],
            "dailyCap": cfg["daily_cap_usd"],
        })

    if not confirmed and cfg["confirm_over_usd"] and est > cfg["confirm_over_usd"]:
        raise AiRefused({
            "error": "This is a big one.",
            "reason": "confirm",
            "estimateUsd": round(est, 4),
            "inputTokens": in_tokens,
            "maxOutputTokens": max_tokens,
            "spentToday": used["usd"],
            "dailyCap": cfg["daily_cap_usd"],
        }, status=409)

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=cfg["model"],
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        raise AiRefused({"error": "The Anthropic API key is missing or was rejected."}, 503)
    except anthropic.RateLimitError:
        raise AiRefused({"error": "Rate limited by Anthropic. Wait a moment and try again."}, 429)
    except anthropic.APIConnectionError:
        raise AiRefused({"error": "Could not reach the Anthropic API."}, 502)
    except anthropic.APIStatusError as e:
        raise AiRefused({"error": f"Anthropic error: {e.message}"}, 502)
    except Exception as e:
        raise AiRefused({"error": f"AI call failed ({e})."}, 503)

    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    usage = getattr(response, "usage", None)
    record_usage(
        conn, kind, cfg["model"],
        getattr(usage, "input_tokens", in_tokens) if usage else in_tokens,
        getattr(usage, "output_tokens", 0) if usage else estimate_tokens(text),
    )
    if not text:
        raise AiRefused({"error": "The model returned nothing. Try again, or add more detail."}, 502)
    return text


# ---------------------------------------------------------------------------
# Context: what the model is allowed to read
# ---------------------------------------------------------------------------
PER_SOURCE_CHARS = 6000
TOTAL_CONTEXT_CHARS = 60000


def strip_html(html):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def collect_sources(conn, selection, class_id=None, item_id=None):
    """Turn a selection into readable text plus a record of where it came from.

    `selection` may name materials, notes, note folders and syllabus topics. When
    it is empty, fall back to what looks relevant for the class: extracted file
    text, then notes, newest first.
    """
    selection = selection or {}
    chosen_materials = selection.get("materialIds") or []
    chosen_notes = selection.get("noteIds") or []
    chosen_folders = selection.get("folderIds") or []
    chosen_syllabus = selection.get("syllabusIds") or []
    auto = not any([chosen_materials, chosen_notes, chosen_folders, chosen_syllabus])

    # a folder means every note inside it, and inside its subfolders
    if chosen_folders:
        pending = list(chosen_folders)
        seen = set()
        while pending:
            fid = pending.pop()
            if fid in seen:
                continue
            seen.add(fid)
            for r in conn.execute("SELECT id FROM note_folders WHERE parent_id=?", (fid,)).fetchall():
                pending.append(r["id"])
        rows = conn.execute(
            "SELECT id FROM notes WHERE folder_id IN (%s) AND (deleted_at IS NULL OR deleted_at='')"
            % ",".join("?" * len(seen)),
            tuple(seen),
        ).fetchall()
        chosen_notes = list(chosen_notes) + [r["id"] for r in rows]

    parts, used = [], []
    budget = TOTAL_CONTEXT_CHARS

    seen = set()

    def add(kind, ref_id, title, body):
        nonlocal budget
        body = (body or "").strip()
        if not body or budget <= 0 or (kind, ref_id) in seen:
            return
        seen.add((kind, ref_id))
        chunk = body[:min(PER_SOURCE_CHARS, budget)]
        parts.append(f"--- {kind}: {title} ---\n{chunk}")
        used.append({"type": kind, "id": ref_id, "title": title, "chars": len(chunk)})
        budget -= len(chunk)

    # Working on an assignment, its own files and the notes about it come first; the
    # rest of the class only fills whatever room is left.
    if auto and item_id:
        for m in conn.execute(
                "SELECT m.id, m.title, m.filename, m.extracted_text FROM item_files f "
                "JOIN materials m ON m.id=f.material_id WHERE f.item_id=?", (item_id,)).fetchall():
            add("file", m["id"], m["title"] or m["filename"] or "File", m["extracted_text"])
        for n in conn.execute(
                "SELECT id, title, text FROM notes WHERE (deleted_at IS NULL OR deleted_at='') AND "
                "(linked_item_id=? OR id IN (SELECT note_id FROM note_links WHERE item_id=?))",
                (item_id, item_id)).fetchall():
            add("note", n["id"], n["title"] or "Untitled note", strip_html(n["text"]))

    if auto and class_id:
        for m in conn.execute(
            "SELECT id, title, filename, extracted_text FROM materials "
            "WHERE class_id=? AND extracted_text IS NOT NULL AND extracted_text != '' "
            "ORDER BY created_at DESC LIMIT 5", (class_id,)).fetchall():
            add("file", m["id"], m["title"] or m["filename"] or "File", m["extracted_text"])
        for n in conn.execute(
            "SELECT id, title, text FROM notes WHERE class_id=? "
            "AND (deleted_at IS NULL OR deleted_at='') ORDER BY updated_at DESC LIMIT 5",
            (class_id,)).fetchall():
            add("note", n["id"], n["title"] or "Untitled note", strip_html(n["text"]))
    else:
        for mid in chosen_materials:
            m = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
            if m:
                add("file", m["id"], m["title"] or m["filename"] or "File", m["extracted_text"])
        for nid in dict.fromkeys(chosen_notes):
            n = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
            if n:
                add("note", n["id"], n["title"] or "Untitled note", strip_html(n["text"]))
        for sid in chosen_syllabus:
            t = conn.execute("SELECT * FROM syllabus_topics WHERE id=?", (sid,)).fetchone()
            if t:
                add("syllabus topic", t["id"], t["title"] or "Topic", t["title"])

    # the assignment's own rubric is always worth including
    if item_id:
        r = conn.execute("SELECT * FROM rubrics WHERE item_id=?", (item_id,)).fetchone()
        if r and r["criteria"]:
            try:
                crit = json.loads(r["criteria"])
                lines = []
                for c in crit:
                    line = "- " + (c.get("name") or "")
                    if c.get("points") is not None:
                        line += f" ({c['points']} pts)"
                    if c.get("description"):
                        line += ": " + c["description"]
                    lines.append(line)
                if lines:
                    add("rubric", r["id"], "Marking rubric", "\n".join(lines))
            except Exception:
                pass

    return "\n\n".join(parts), used


def assignment_brief(conn, item_id):
    if not item_id:
        return "", None, None
    it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        return "", None, None
    cls = conn.execute("SELECT * FROM classes WHERE id=?", (it["class_id"],)).fetchone() if it["class_id"] else None
    bits = [f"Assignment: {it['title'] or 'Untitled'}", f"Type: {it['type'] or 'assignment'}"]
    if cls:
        bits.append(f"Course: {(cls['code'] or '')} {(cls['name'] or '')}".strip())
    if it["due_date"]:
        bits.append(f"Due: {it['due_date']}")
    if it["weight"] is not None:
        bits.append(f"Worth: {it['weight']}% of the course grade")
    if it["notes"]:
        bits.append("What the student recorded about it:\n" + it["notes"])
    return "\n".join(bits), it, cls


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
SCAFFOLD_NOTE = (
    "\n\nThis is study scaffolding for the student to work from and revise. It is not "
    "something to hand in as-is. Where the student needs to supply their own specifics "
    "(their data, their examples, their citations), say so plainly rather than inventing them."
)

TOOLS = {
    "outline": {
        "label": "Build an assignment outline",
        "max_tokens": 3000,
        "prompt": "Produce a working outline for this assignment: the shape of the argument, "
                  "the sections, and what belongs in each. Show where evidence is needed.",
    },
    "rubric": {
        "label": "Break down the rubric",
        "max_tokens": 3000,
        "prompt": "Work through the marking rubric criterion by criterion. For each one say what "
                  "the marker is actually looking for, what full marks would look like, and the "
                  "most common way students lose points on it.",
    },
    "study_plan": {
        "label": "Create a study plan",
        "max_tokens": 3000,
        "prompt": "Build a realistic study plan working back from the due date. Break it into "
                  "sessions with a clear objective each, order them so earlier work feeds later "
                  "work, and be honest about how long each will take.",
    },
    "explain": {
        "label": "Explain the instructions",
        "max_tokens": 2000,
        "prompt": "Explain in plain language what this assignment is actually asking for. "
                  "Separate what is required from what is optional, and name anything ambiguous "
                  "that is worth asking the instructor about.",
    },
    "summarize": {
        "label": "Summarise the material",
        "max_tokens": 3000,
        "prompt": "Summarise the material provided. Lead with the argument or main claim, then "
                  "the supporting points, then anything the student should be sceptical of.",
    },
    "concepts": {
        "label": "Find the important concepts",
        "max_tokens": 2500,
        "prompt": "From the material provided, pull out the concepts worth knowing. For each: the "
                  "term, a one-line definition in plain language, and why it matters in this course.",
    },
    "gaps": {
        "label": "Identify gaps",
        "max_tokens": 2500,
        "prompt": "Compare the student's notes against the assignment and the course material, and "
                  "say what is missing or thin. Be specific about which topics are underdeveloped "
                  "and what to go and read. Do not pad the list; if the notes are solid, say so.",
    },
    "draft": {
        "label": "Draft assistance",
        "max_tokens": 6000,
        "prompt": "Write a first draft the student can build on. Use a natural voice appropriate "
                  "for an undergraduate. Mark clearly, in a short closing list, every place where "
                  "the student must add their own material.",
    },
    "revise": {
        "label": "Revision assistance",
        "max_tokens": 5000,
        "prompt": "Review the student's draft below. Say what is working, what is not, and what to "
                  "change, in that order. Be concrete: quote the line and show the fix. Do not "
                  "rewrite the whole thing unless asked.",
    },
    "refine": {
        "label": "Writing refinement",
        "max_tokens": 5000,
        "prompt": "Improve the wording of the text below so it reads more clearly and naturally, "
                  "while keeping the author's meaning, argument, structure and level of formality "
                  "intact. Do not add claims, evidence or citations that are not already there. "
                  "After the revised text, list the substantive changes you made and why.",
    },
}


@bp.route("/api/ai/tools")
def list_tools():
    return jsonify([{"key": k, "label": v["label"]} for k, v in TOOLS.items()])


@bp.route("/api/ai/settings", methods=["GET", "PUT"])
def ai_settings():
    conn = get_db()
    if request.method == "PUT":
        cfg = save_settings(conn, request.get_json(force=True) or {})
    else:
        cfg = settings(conn)
    out = dict(cfg)
    out["usage"] = spent_today(conn, cfg)
    conn.close()
    return jsonify(out)


@bp.route("/api/ai/context/<cid>")
def ai_context(cid):
    """Everything in a class that could be fed to a tool, so the user can pick."""
    conn = get_db()
    mats = conn.execute(
        "SELECT id, title, filename, category, "
        "  CASE WHEN extracted_text IS NULL OR extracted_text='' THEN 0 ELSE 1 END AS readable, "
        "  LENGTH(COALESCE(extracted_text,'')) AS chars "
        "FROM materials WHERE class_id=? ORDER BY created_at DESC", (cid,)).fetchall()
    notes = conn.execute(
        "SELECT id, title, folder_id, LENGTH(COALESCE(text,'')) AS chars FROM notes "
        "WHERE class_id=? AND (deleted_at IS NULL OR deleted_at='') ORDER BY updated_at DESC",
        (cid,)).fetchall()
    # the note count travels with the folder so the picker can say which folders
    # would actually contribute something
    folders = conn.execute(
        "SELECT f.id, f.name, f.parent_id, f.kind, "
        "  (SELECT COUNT(*) FROM notes n WHERE n.folder_id=f.id "
        "     AND (n.deleted_at IS NULL OR n.deleted_at='') "
        "     AND COALESCE(n.text,'') != '') AS n_notes "
        "FROM note_folders f WHERE f.class_id=? ORDER BY f.name", (cid,)).fetchall()
    topics = conn.execute(
        "SELECT id, title, done FROM syllabus_topics WHERE class_id=? ORDER BY sort_order", (cid,)).fetchall()
    conn.close()
    return jsonify({
        "files": [{"id": m["id"], "title": m["title"] or m["filename"] or "File",
                   "category": m["category"], "readable": bool(m["readable"]),
                   "chars": m["chars"]} for m in mats],
        "notes": [{"id": n["id"], "title": n["title"] or "Untitled note",
                   "folderId": n["folder_id"], "chars": n["chars"]} for n in notes],
        "folders": [{"id": f["id"], "name": f["name"], "parentId": f["parent_id"],
                     "kind": f["kind"], "noteCount": f["n_notes"]} for f in folders],
        "syllabus": [{"id": t["id"], "title": t["title"], "done": bool(t["done"])} for t in topics],
    })


@bp.route("/api/ai/estimate", methods=["POST"])
def ai_estimate():
    """What a run would cost, before running it."""
    data = request.get_json(force=True) or {}
    conn = get_db()
    cfg = settings(conn)
    context, used = collect_sources(conn, data.get("selection"), data.get("classId"), data.get("itemId"))
    brief, _, _ = assignment_brief(conn, data.get("itemId"))
    tool = TOOLS.get(data.get("tool") or "explain", TOOLS["explain"])
    prompt_len = len(context) + len(brief) + len(tool["prompt"]) + len(data.get("text") or "")
    in_tokens = estimate_tokens("x" * prompt_len)
    out = {
        "inputTokens": in_tokens,
        "maxOutputTokens": tool["max_tokens"],
        "estimateUsd": round(estimate_cost(cfg, in_tokens, tool["max_tokens"]), 4),
        "sources": used,
        "spentToday": spent_today(conn, cfg),
        "dailyCap": cfg["daily_cap_usd"],
        "confirmOver": cfg["confirm_over_usd"],
    }
    conn.close()
    return jsonify(out)


# ---------------------------------------------------------------------------
# Running a tool
# ---------------------------------------------------------------------------
def build_tool_prompt(tool_key, brief, context, user_text, extra):
    tool = TOOLS[tool_key]
    parts = [tool["prompt"]]
    if brief:
        parts += ["", brief]
    if user_text:
        label = "The student's draft:" if tool_key in ("revise", "refine") else "Text supplied by the student:"
        parts += ["", label, user_text]
    if context:
        parts += ["", "Course material the student selected:", context]
    if extra:
        parts += ["", "Additional instructions from the student:", extra[:4000]]
    parts.append(SCAFFOLD_NOTE)
    return "\n".join(parts)


@bp.route("/api/ai/run", methods=["POST"])
def ai_run():
    """Run one Headstart tool and hand back the text. Nothing is written elsewhere."""
    data = request.get_json(force=True) or {}
    tool_key = data.get("tool")
    if tool_key not in TOOLS:
        return jsonify({"error": "Unknown tool."}), 400

    conn = get_db()
    try:
        brief, item, cls = assignment_brief(conn, data.get("itemId"))
        class_id = data.get("classId") or (item["class_id"] if item else None)
        context, used = collect_sources(conn, data.get("selection"), class_id, data.get("itemId"))
        user_text = (data.get("text") or "").strip()

        if tool_key in ("revise", "refine") and not user_text:
            return jsonify({"error": "Paste the writing you want me to work on first."}), 400

        prompt = build_tool_prompt(tool_key, brief, context, user_text, data.get("instructions"))
        content = call_claude(conn, tool_key, prompt,
                              TOOLS[tool_key]["max_tokens"], bool(data.get("confirmed")))

        saved_id = None
        # The six original kinds still live on the assignment so the rest of the app
        # keeps working; the newer tools are transient results.
        legacy = {"outline": "essay_outline", "explain": "explain", "draft": "draft",
                  "study_plan": "study_outline", "summarize": "synthesis"}
        if item and tool_key in legacy:
            kind = legacy[tool_key]
            now = datetime.utcnow().isoformat()
            existing = conn.execute(
                "SELECT id FROM headstarts WHERE item_id=? AND kind=?", (item["id"], kind)).fetchone()
            if existing:
                saved_id = existing["id"]
                conn.execute("UPDATE headstarts SET content=?, status='ready', updated_at=? WHERE id=?",
                             (content, now, saved_id))
            else:
                saved_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO headstarts (id, item_id, kind, content, status, instructions, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (saved_id, item["id"], kind, content, "ready", "", now, now))
            for src in used:
                conn.execute(
                    "INSERT INTO headstart_sources (id, headstart_id, material_id, note_id, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (str(uuid.uuid4()), saved_id,
                     src["id"] if src["type"] == "file" else None,
                     src["id"] if src["type"] == "note" else None, now))
            conn.commit()

        return jsonify({"content": content, "sources": used, "savedId": saved_id,
                        "usage": spent_today(conn, settings(conn))})
    except AiRefused as e:
        return jsonify(e.payload), e.status
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Quiz Me and practice tests
# ---------------------------------------------------------------------------
QUESTION_KINDS = {
    "multiple_choice": "multiple choice with four options",
    "true_false": "true or false",
    "short_answer": "short answer, one or two sentences",
    "concept": "a concept or application question that requires reasoning, not recall",
}

QUIZ_FORMAT = """
Return ONLY a JSON array, no prose before or after. Each element:
{"kind": "multiple_choice|true_false|short_answer|concept",
 "prompt": "the question",
 "choices": ["A","B","C","D"],
 "answer": "the correct answer, matching one of the choices exactly for multiple choice",
 "explanation": "why that is the answer, in one or two sentences"}
For true_false use choices ["True","False"]. For short_answer and concept use an empty choices array.
Base every question on the material provided. Do not invent facts that are not in it.
"""


def parse_json_array(text):
    """Models sometimes wrap JSON in prose or a fence. Dig it out."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array in the response")
    return json.loads(text[start:end + 1])


@bp.route("/api/ai/quiz", methods=["POST"])
def ai_quiz():
    data = request.get_json(force=True) or {}
    count = max(1, min(40, int(data.get("count") or 10)))
    kinds = [k for k in (data.get("kinds") or ["multiple_choice"]) if k in QUESTION_KINDS] or ["multiple_choice"]
    difficulty = data.get("difficulty") or "mixed"
    is_test = bool(data.get("practiceTest"))

    conn = get_db()
    try:
        brief, item, cls = assignment_brief(conn, data.get("itemId"))
        class_id = data.get("classId") or (item["class_id"] if item else None)
        context, used = collect_sources(conn, data.get("selection"), class_id, data.get("itemId"))
        if not context.strip():
            return jsonify({"error": "Pick some material first. There is nothing to write questions from."}), 400

        parts = [
            ("Write a practice exam of " if is_test else "Write a quiz of ")
            + f"{count} questions at {difficulty} difficulty.",
            "Use these question types: " + ", ".join(QUESTION_KINDS[k] for k in kinds) + ".",
        ]
        if is_test and data.get("modelAfter"):
            parts.append("Model the style and phrasing of the questions on the past paper included below.")
        if brief:
            parts += ["", brief]
        parts += ["", "Material to draw on:", context, QUIZ_FORMAT]
        raw = call_claude(conn, "quiz", "\n".join(parts),
                          min(8000, 400 * count), bool(data.get("confirmed")))
        try:
            questions = parse_json_array(raw)
        except Exception:
            return jsonify({"error": "The model did not return usable questions. Try again."}), 502

        now = datetime.utcnow().isoformat()
        qid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO quizzes (id, semester_id, class_id, item_id, title, kind, difficulty, source, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (qid, semester_for(conn, class_id), class_id, item["id"] if item else None,
             data.get("title") or (("Practice exam" if is_test else "Quiz") +
                                   (" · " + (cls["code"] or "") if cls else "")),
             "practice_test" if is_test else "quiz", difficulty, "headstart", now, now))
        for i, q in enumerate(questions):
            conn.execute(
                "INSERT INTO quiz_questions (id, quiz_id, kind, prompt, choices, answer, explanation, sort_order)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), qid,
                 q.get("kind") if q.get("kind") in QUESTION_KINDS else "multiple_choice",
                 q.get("prompt") or "", json.dumps(q.get("choices") or []),
                 q.get("answer") or "", q.get("explanation") or "", i))
        conn.commit()
        return jsonify({"id": qid, "count": len(questions), "sources": used,
                        "usage": spent_today(conn, settings(conn))}), 201
    except AiRefused as e:
        return jsonify(e.payload), e.status
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Flashcards
# ---------------------------------------------------------------------------
CARD_FORMAT = """
Return ONLY a JSON array, no prose. Each element:
{"front": "term, question or concept", "back": "definition, answer or explanation",
 "kind": "term|question|concept"}
Keep the front short enough to read at a glance. Put the substance on the back.
Base every card on the material provided; do not invent content.
"""


@bp.route("/api/ai/flashcards", methods=["POST"])
def ai_flashcards():
    data = request.get_json(force=True) or {}
    count = max(1, min(80, int(data.get("count") or 20)))
    style = data.get("style") or "mixed"

    conn = get_db()
    try:
        class_id = data.get("classId")
        context, used = collect_sources(conn, data.get("selection"), class_id, data.get("itemId"))
        if not context.strip():
            return jsonify({"error": "Pick some material first. There is nothing to make cards from."}), 400

        style_line = {
            "term": "Every card should be term on the front, definition on the back.",
            "question": "Every card should be a question on the front, the answer on the back.",
            "concept": "Every card should be a concept on the front, an explanation on the back.",
        }.get(style, "Mix term/definition, question/answer and concept/explanation as suits the material.")

        prompt = "\n".join([
            f"Make {count} flashcards from the material below.", style_line,
            "", "Material:", context, CARD_FORMAT,
        ])
        raw = call_claude(conn, "flashcards", prompt, min(8000, 150 * count), bool(data.get("confirmed")))
        try:
            cards = parse_json_array(raw)
        except Exception:
            return jsonify({"error": "The model did not return usable cards. Try again."}), 502

        now = datetime.utcnow().isoformat()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        did = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO flashcard_decks (id, semester_id, class_id, name, description, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (did, semester_for(conn, class_id), class_id, data.get("name") or "Generated deck",
             "From " + ", ".join(s["title"] for s in used[:3]) if used else "", now, now))
        for i, c in enumerate(cards):
            conn.execute(
                "INSERT INTO flashcards (id, deck_id, front, back, kind, due_date, sort_order, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), did, c.get("front") or "", c.get("back") or "",
                 c.get("kind") if c.get("kind") in ("term", "question", "concept") else "term",
                 today, i, now))
        conn.commit()
        return jsonify({"id": did, "count": len(cards), "sources": used,
                        "usage": spent_today(conn, settings(conn))}), 201
    except AiRefused as e:
        return jsonify(e.payload), e.status
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Taking a quiz
#
# Answers do not travel to the browser until the attempt is submitted. A practice
# test is meant to be sat, not read, and a page that ships the answer key alongside
# the questions is not a test.
#
# Multiple choice and true/false are graded here. Short answer and concept questions
# cannot be marked by string comparison without being wrong often enough to be
# annoying, so those come back unmarked with the expected answer, and the student
# says whether they got it. That self-assessment is sent on a second submit and
# folded into the same attempt.
# ---------------------------------------------------------------------------
OBJECTIVE_KINDS = ("multiple_choice", "true_false")


def norm_answer(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def quiz_row(conn, qid):
    q = conn.execute("SELECT * FROM quizzes WHERE id=?", (qid,)).fetchone()
    if not q:
        abort(404)
    return q


def quiz_json(conn, q, with_answers=False):
    rows = conn.execute(
        "SELECT * FROM quiz_questions WHERE quiz_id=? ORDER BY sort_order", (q["id"],)).fetchall()
    questions = []
    for r in rows:
        item = {"id": r["id"], "kind": r["kind"], "prompt": r["prompt"],
                "choices": json.loads(r["choices"] or "[]"),
                "objective": r["kind"] in OBJECTIVE_KINDS}
        if with_answers:
            item["answer"] = r["answer"]
            item["explanation"] = r["explanation"]
        questions.append(item)
    return {"id": q["id"], "classId": q["class_id"], "itemId": q["item_id"],
            "title": q["title"], "kind": q["kind"], "difficulty": q["difficulty"],
            "createdAt": q["created_at"], "questions": questions}


@bp.route("/api/quizzes")
def list_quizzes():
    conn = get_db()
    where, args = " WHERE q.semester_id=?", [active_semester_id(conn)]
    if request.args.get("classId"):
        where, args = " WHERE q.class_id=?", [request.args["classId"]]
    rows = conn.execute(
        "SELECT q.*, "
        " (SELECT COUNT(*) FROM quiz_questions qq WHERE qq.quiz_id=q.id) AS n_questions, "
        " (SELECT COUNT(*) FROM quiz_attempts a WHERE a.quiz_id=q.id AND a.completed_at IS NOT NULL) AS n_attempts, "
        " (SELECT MAX(a.score) FROM quiz_attempts a WHERE a.quiz_id=q.id) AS best, "
        " (SELECT MAX(a.completed_at) FROM quiz_attempts a WHERE a.quiz_id=q.id) AS last_taken "
        "FROM quizzes q" + where + " ORDER BY q.created_at DESC", args).fetchall()
    conn.close()
    return jsonify([{"id": r["id"], "classId": r["class_id"], "itemId": r["item_id"],
                     "title": r["title"], "kind": r["kind"], "difficulty": r["difficulty"],
                     "questionCount": r["n_questions"], "attemptCount": r["n_attempts"],
                     "bestScore": r["best"], "lastTaken": r["last_taken"],
                     "createdAt": r["created_at"]} for r in rows])


@bp.route("/api/quizzes/<qid>", methods=["GET", "DELETE"])
def one_quiz(qid):
    conn = get_db()
    q = quiz_row(conn, qid)
    if request.method == "DELETE":
        conn.execute("DELETE FROM quizzes WHERE id=?", (qid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    # Answers only once something has been submitted, or on explicit review.
    out = quiz_json(conn, q, with_answers=request.args.get("answers") == "1")
    conn.close()
    return jsonify(out)


@bp.route("/api/quizzes/<qid>/attempts", methods=["GET", "POST"])
def quiz_attempts(qid):
    conn = get_db()
    quiz_row(conn, qid)

    if request.method == "GET":
        rows = conn.execute(
            "SELECT * FROM quiz_attempts WHERE quiz_id=? AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC", (qid,)).fetchall()
        conn.close()
        return jsonify([{"id": r["id"], "score": r["score"], "correct": r["correct_count"],
                         "total": r["total_count"], "startedAt": r["started_at"],
                         "completedAt": r["completed_at"],
                         "responses": json.loads(r["responses"] or "{}")} for r in rows])

    data = request.get_json(force=True) or {}
    given = data.get("responses") or {}
    questions = conn.execute(
        "SELECT * FROM quiz_questions WHERE quiz_id=? ORDER BY sort_order", (qid,)).fetchall()

    results, correct, graded = [], 0, 0
    for r in questions:
        raw = given.get(r["id"])
        if isinstance(raw, dict):
            value, self_correct = raw.get("value"), raw.get("selfCorrect")
        else:
            value, self_correct = raw, None

        if r["kind"] in OBJECTIVE_KINDS:
            is_right = norm_answer(value) == norm_answer(r["answer"]) if value is not None else False
        elif self_correct is None:
            is_right = None          # waiting on the student to mark it
        else:
            is_right = bool(self_correct)

        if is_right is not None:
            graded += 1
            if is_right:
                correct += 1
        results.append({"id": r["id"], "kind": r["kind"], "prompt": r["prompt"],
                        "given": value, "answer": r["answer"],
                        "explanation": r["explanation"], "correct": is_right,
                        "objective": r["kind"] in OBJECTIVE_KINDS})

    total = len(questions)
    score = round(100.0 * correct / graded, 1) if graded else None
    now = datetime.utcnow().isoformat()
    aid = data.get("attemptId")
    payload = json.dumps(given)

    if aid and conn.execute("SELECT 1 FROM quiz_attempts WHERE id=?", (aid,)).fetchone():
        conn.execute(
            "UPDATE quiz_attempts SET responses=?, score=?, correct_count=?, total_count=?, completed_at=?"
            " WHERE id=?", (payload, score, correct, total, now, aid))
    else:
        aid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO quiz_attempts (id, quiz_id, responses, score, correct_count, total_count,"
            " started_at, completed_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, qid, payload, score, correct, total, data.get("startedAt") or now, now))
    conn.commit()
    conn.close()
    return jsonify({"attemptId": aid, "score": score, "correct": correct,
                    "graded": graded, "total": total, "results": results}), 201


# ---------------------------------------------------------------------------
# Flashcards
#
# Scheduling is SM-2, the algorithm behind Anki and SuperMemo. A grade of 0-2 is a
# miss and sends the card back to the start of the ladder; 3-5 is a hit and pushes
# the next sighting further out, scaled by how easy the card has proven to be.
# ---------------------------------------------------------------------------
def today_str():
    return datetime.utcnow().strftime("%Y-%m-%d")


def card_json(r):
    return {"id": r["id"], "deckId": r["deck_id"], "front": r["front"], "back": r["back"],
            "kind": r["kind"], "ease": r["ease"], "interval": r["interval_days"],
            "repetitions": r["repetitions"], "lapses": r["lapses"], "due": r["due_date"],
            "lastReviewed": r["last_reviewed_at"], "suspended": bool(r["suspended"]),
            "sortOrder": r["sort_order"]}


@bp.route("/api/decks", methods=["GET", "POST"])
def decks():
    conn = get_db()
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        now = datetime.utcnow().isoformat()
        did = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO flashcard_decks (id, semester_id, class_id, name, description, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (did, semester_for(conn, data.get("classId")), data.get("classId"),
             (data.get("name") or "New deck").strip(),
             data.get("description") or "", now, now))
        conn.commit()
        conn.close()
        return jsonify({"id": did}), 201

    where, args = " WHERE d.semester_id=?", [active_semester_id(conn)]
    if request.args.get("classId"):
        where, args = " WHERE d.class_id=?", [request.args["classId"]]
    rows = conn.execute(
        "SELECT d.*, "
        " (SELECT COUNT(*) FROM flashcards c WHERE c.deck_id=d.id) AS n_cards, "
        " (SELECT COUNT(*) FROM flashcards c WHERE c.deck_id=d.id AND c.suspended=0"
        "   AND (c.due_date IS NULL OR c.due_date<=?)) AS n_due, "
        " (SELECT COUNT(*) FROM flashcards c WHERE c.deck_id=d.id AND c.repetitions=0) AS n_new "
        "FROM flashcard_decks d" + where + " ORDER BY d.updated_at DESC",
        [today_str()] + args).fetchall()
    conn.close()
    return jsonify([{"id": r["id"], "classId": r["class_id"], "name": r["name"],
                     "description": r["description"], "cardCount": r["n_cards"],
                     "dueCount": r["n_due"], "newCount": r["n_new"],
                     "createdAt": r["created_at"], "updatedAt": r["updated_at"]} for r in rows])


@bp.route("/api/decks/<did>", methods=["GET", "PUT", "DELETE"])
def one_deck(did):
    conn = get_db()
    d = conn.execute("SELECT * FROM flashcard_decks WHERE id=?", (did,)).fetchone()
    if not d:
        conn.close()
        abort(404)

    if request.method == "DELETE":
        conn.execute("DELETE FROM flashcard_decks WHERE id=?", (did,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    if request.method == "PUT":
        data = request.get_json(force=True) or {}
        for key, col in (("name", "name"), ("description", "description"), ("classId", "class_id")):
            if key in data:
                conn.execute(f"UPDATE flashcard_decks SET {col}=? WHERE id=?", (data[key], did))
        conn.execute("UPDATE flashcard_decks SET updated_at=? WHERE id=?",
                     (datetime.utcnow().isoformat(), did))
        conn.commit()
        d = conn.execute("SELECT * FROM flashcard_decks WHERE id=?", (did,)).fetchone()

    cards = conn.execute(
        "SELECT * FROM flashcards WHERE deck_id=? ORDER BY sort_order, created_at", (did,)).fetchall()
    conn.close()
    return jsonify({"id": d["id"], "classId": d["class_id"], "name": d["name"],
                    "description": d["description"], "createdAt": d["created_at"],
                    "cards": [card_json(c) for c in cards]})


@bp.route("/api/decks/<did>/cards", methods=["POST"])
def add_card(did):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM flashcard_decks WHERE id=?", (did,)).fetchone():
        conn.close()
        abort(404)
    data = request.get_json(force=True) or {}
    now = datetime.utcnow().isoformat()
    nxt = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM flashcards WHERE deck_id=?",
                       (did,)).fetchone()["n"]
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO flashcards (id, deck_id, front, back, kind, due_date, sort_order, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (cid, did, data.get("front") or "", data.get("back") or "",
         data.get("kind") if data.get("kind") in ("term", "question", "concept") else "term",
         today_str(), nxt, now))
    conn.execute("UPDATE flashcard_decks SET updated_at=? WHERE id=?", (now, did))
    conn.commit()
    row = conn.execute("SELECT * FROM flashcards WHERE id=?", (cid,)).fetchone()
    conn.close()
    return jsonify(card_json(row)), 201


@bp.route("/api/cards/<cid>", methods=["PUT", "DELETE"])
def one_card(cid):
    conn = get_db()
    c = conn.execute("SELECT * FROM flashcards WHERE id=?", (cid,)).fetchone()
    if not c:
        conn.close()
        abort(404)
    if request.method == "DELETE":
        conn.execute("DELETE FROM flashcards WHERE id=?", (cid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    data = request.get_json(force=True) or {}
    for key, col in (("front", "front"), ("back", "back"), ("kind", "kind"),
                     ("sortOrder", "sort_order")):
        if key in data:
            conn.execute(f"UPDATE flashcards SET {col}=? WHERE id=?", (data[key], cid))
    if "suspended" in data:
        conn.execute("UPDATE flashcards SET suspended=? WHERE id=?",
                     (1 if data["suspended"] else 0, cid))
    conn.execute("UPDATE flashcard_decks SET updated_at=? WHERE id=?",
                 (datetime.utcnow().isoformat(), c["deck_id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM flashcards WHERE id=?", (cid,)).fetchone()
    conn.close()
    return jsonify(card_json(row))


@bp.route("/api/cards/<cid>/review", methods=["POST"])
def review_card(cid):
    """Record how a card went and schedule the next sighting (SM-2)."""
    conn = get_db()
    c = conn.execute("SELECT * FROM flashcards WHERE id=?", (cid,)).fetchone()
    if not c:
        conn.close()
        abort(404)
    grade = max(0, min(5, int((request.get_json(force=True) or {}).get("grade", 3))))

    ease = c["ease"] or 2.5
    reps = c["repetitions"] or 0
    interval = c["interval_days"] or 0
    lapses = c["lapses"] or 0

    if grade < 3:
        reps, interval, lapses = 0, 1, lapses + 1
    else:
        reps += 1
        interval = 1 if reps == 1 else (6 if reps == 2 else max(1, round(interval * ease)))
        ease = max(1.3, ease + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)))

    now = datetime.utcnow()
    due = (now + timedelta(days=interval)).strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE flashcards SET ease=?, interval_days=?, repetitions=?, lapses=?, due_date=?,"
        " last_reviewed_at=? WHERE id=?",
        (round(ease, 3), interval, reps, lapses, due, now.isoformat(), cid))
    conn.commit()
    row = conn.execute("SELECT * FROM flashcards WHERE id=?", (cid,)).fetchone()
    conn.close()
    return jsonify(card_json(row))

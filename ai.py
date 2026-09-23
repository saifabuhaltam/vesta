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
import time
import re
import sqlite3
import uuid
from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, jsonify, request, stream_with_context

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

# The ceiling across every account, in dollars a day. Unset means no global limit.
#
# This is an environment variable and not a setting, because `daily_cap_usd` lives in
# `app_settings`, which is per user and writable from the app: anyone can raise their
# own ceiling through PUT /api/ai/settings. That makes the per-account cap a courtesy
# to yourself, not a spend control. Every call is billed to one Anthropic key, so the
# only number that protects the person paying is one the people spending cannot edit.
GLOBAL_CAP_USD = float(os.environ.get("AI_GLOBAL_DAILY_CAP_USD") or 0) or None

# Totalling every account costs one connection per account, so the answer is held for
# a minute. The cost of that staleness is bounded: at worst, a minute of concurrent
# spending crosses the line together before any of them are refused.
GLOBAL_CACHE_SECONDS = 60
_global_spend = {"day": None, "usd": 0.0, "at": 0.0}


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


def spent_today_everyone(cfg):
    """Today's spend across every account, or None where there are no accounts.

    Row level security is the reason this cannot be one `sum()`: `ai_usage` is filtered
    to the signed-in user, and an ownerless connection matches nothing at all, so the
    total has to be gathered one account at a time.
    """
    import db
    if not db.DATABASE_URL:
        return None
    day = datetime.utcnow().strftime("%Y-%m-%d")
    now = time.time()
    if _global_spend["day"] == day and (now - _global_spend["at"]) < GLOBAL_CACHE_SECONDS:
        return _global_spend["usd"]
    total = 0.0
    for uid in db.all_user_ids():
        conn = db.get_db(user_id=uid)
        try:
            total += spent_today(conn, cfg)["usd"]
        except Exception:
            continue
        finally:
            conn.close()
    _global_spend.update(day=day, usd=round(total, 4), at=now)
    return _global_spend["usd"]


def record_usage(conn, kind, model, in_tokens, out_tokens, cache_read=0, cache_write=0):
    # A spend just happened, so the cached global total is now wrong. Cheaper to
    # invalidate than to hold a number that lets the next call through wrongly.
    _global_spend["at"] = 0.0
    conn.execute(
        "INSERT INTO ai_usage (id, kind, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, day, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), kind, model, in_tokens, out_tokens, cache_read, cache_write,
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

    everyone = spent_today_everyone(cfg)
    if GLOBAL_CAP_USD and everyone is not None and everyone >= GLOBAL_CAP_USD:
        raise AiRefused({
            "error": "Vesta has reached today's AI limit across all accounts.",
            "reason": "global_cap",
            "spentTodayEveryone": everyone,
            "globalCap": GLOBAL_CAP_USD,
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


# The standing instruction for a thread. It is the cached prefix's first block, so it
# must not carry anything that changes per turn, or the cache misses every time.
CHAT_SYSTEM = (
    "You are helping a university student with their coursework inside Vesta, their "
    "study app. You are given the course material they have attached to this "
    "conversation, and the conversation so far.\n\n"
    "Work from the attached material. Where it does not cover something, say so rather "
    "than inventing a source, a citation or a fact. Anything you produce is scaffolding "
    "for the student to revise and build on, not work to hand in as it stands; where "
    "they need to supply their own specifics, say so plainly.\n\n"
    "Keep continuity with what has already been said in this conversation. If they ask "
    "for a second piece of work like an earlier one, do not repeat the earlier one's "
    "points unless they ask you to."
)


def chat_system(context):
    """The standing instruction, then the pinned material behind a cache breakpoint."""
    system = [{"type": "text", "text": CHAT_SYSTEM}]
    if context:
        system.append({
            "type": "text",
            "text": "Course material the student attached to this conversation:\n\n" + context,
            "cache_control": {"type": "ephemeral"},
        })
    return system


def chat_guard(conn, context, history, max_tokens, confirmed):
    """Refuse a thread turn before it costs anything: the caps, then the ask-first amount.

    Separate from the call so a streamed reply can be refused with an ordinary JSON
    answer before the stream opens, rather than halfway into one.
    """
    cfg = settings(conn)
    prompt_chars = len(CHAT_SYSTEM) + len(context or "") + sum(len(m["content"]) for m in history)
    in_tokens = estimate_tokens("x" * prompt_chars)
    est = estimate_cost(cfg, in_tokens, max_tokens)
    used = spent_today(conn, cfg)

    if cfg["daily_cap_usd"] and used["usd"] >= cfg["daily_cap_usd"]:
        raise AiRefused({"error": "You have reached today's AI limit.", "reason": "daily_cap",
                         "spentToday": used["usd"], "dailyCap": cfg["daily_cap_usd"]})
    everyone = spent_today_everyone(cfg)
    if GLOBAL_CAP_USD and everyone is not None and everyone >= GLOBAL_CAP_USD:
        raise AiRefused({"error": "Vesta has reached today's AI limit across all accounts.",
                         "reason": "global_cap", "spentTodayEveryone": everyone,
                         "globalCap": GLOBAL_CAP_USD})
    if not confirmed and cfg["confirm_over_usd"] and est > cfg["confirm_over_usd"]:
        raise AiRefused({"error": "This is a big one.", "reason": "confirm",
                         "estimateUsd": round(est, 4), "inputTokens": in_tokens,
                         "maxOutputTokens": max_tokens, "spentToday": used["usd"],
                         "dailyCap": cfg["daily_cap_usd"]}, status=409)
    return cfg, in_tokens


def call_claude_chat(conn, kind, context, history, max_tokens=4000, confirmed=False):
    """One turn of a thread: the pinned material, then the conversation so far.

    The material goes in `system` with a cache breakpoint after it. Render order is
    tools, then system, then messages, so caching there means the expensive part of
    the prompt is a cache read on every turn after the first while the conversation
    grows after the breakpoint. A ten-turn thread over 15,000 tokens of readings costs
    roughly $0.10 this way against $0.35 without it.

    `history` is the full conversation including the new user turn, oldest first.
    """
    cfg, in_tokens = chat_guard(conn, context, history, max_tokens, confirmed)
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=cfg["model"],
            max_tokens=max_tokens,
            system=chat_system(context),
            messages=[{"role": m["role"], "content": m["content"]} for m in history],
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
    tin, tout, cread, cwrite = usage_from(response, in_tokens, estimate_tokens(text))
    record_usage(conn, kind, cfg["model"], tin, tout, cread, cwrite)
    if not text:
        raise AiRefused({"error": "The model returned nothing. Try again, or add more detail."}, 502)
    return {"text": text, "inputTokens": tin, "outputTokens": tout,
            "cacheReadTokens": cread, "cacheWriteTokens": cwrite, "model": cfg["model"]}


# Thinking counts against max_tokens, so a thorough turn needs more room than a fast one
# or a long think leaves nothing for the answer.
THOROUGH_MAX_TOKENS = 16000


def stream_claude_chat(conn, cfg, kind, context, history, fast=True, max_tokens=4000):
    """A thread turn as it is written: yields ("thinking", s), ("text", s), then
    ("done", usage), or ("error", payload) if the call fails.

    Measured on a thread-sized prompt (9,300 tokens of material, "draft a discussion
    post"): fast, at low effort, shows its first words in 1.4 seconds; thorough, with
    adaptive thinking, starts thinking at 4 seconds and writing at 19. Without streaming
    both were a blank wait for the whole 26 to 35 seconds.

    Closing the generator part way (the student pressed Stop, or left) closes the
    stream, which stops the generation; what was produced is still recorded, with the
    output estimated from what arrived, because it was still billed.
    """
    request = dict(model=cfg["model"], max_tokens=max_tokens, system=chat_system(context),
                   messages=[{"role": m["role"], "content": m["content"]} for m in history])
    if fast:
        request["output_config"] = {"effort": "low"}
    else:
        request["max_tokens"] = max(max_tokens, THOROUGH_MAX_TOKENS)
        # Summarized, so the screen can show what it is working through while it thinks.
        request["thinking"] = {"type": "adaptive", "display": "summarized"}

    started = {"in": 0, "cread": 0, "cwrite": 0}
    produced = 0
    finished = False
    try:
        with anthropic.Anthropic().messages.stream(**request) as stream:
            for ev in stream:
                if ev.type == "message_start":
                    u = ev.message.usage
                    started.update(**{"in": getattr(u, "input_tokens", 0) or 0,
                                      "cread": getattr(u, "cache_read_input_tokens", 0) or 0,
                                      "cwrite": getattr(u, "cache_creation_input_tokens", 0) or 0})
                elif ev.type == "content_block_delta":
                    if ev.delta.type == "text_delta":
                        produced += len(ev.delta.text)
                        yield ("text", ev.delta.text)
                    elif ev.delta.type == "thinking_delta" and ev.delta.thinking:
                        produced += len(ev.delta.thinking)
                        yield ("thinking", ev.delta.thinking)
            final = stream.get_final_message()
        tin, tout, cread, cwrite = usage_from(final, started["in"], estimate_tokens("x" * produced))
        record_usage(conn, kind, cfg["model"], tin, tout, cread, cwrite)
        finished = True
        yield ("done", {"inputTokens": tin, "outputTokens": tout, "cacheReadTokens": cread,
                        "cacheWriteTokens": cwrite, "model": cfg["model"],
                        "stopReason": final.stop_reason})
    except anthropic.AuthenticationError:
        yield ("error", {"error": "The Anthropic API key is missing or was rejected.", "status": 503})
    except anthropic.RateLimitError:
        yield ("error", {"error": "Rate limited by Anthropic. Wait a moment and try again.", "status": 429})
    except anthropic.APIConnectionError:
        yield ("error", {"error": "Lost the connection to the Anthropic API.", "status": 502})
    except anthropic.APIStatusError as e:
        yield ("error", {"error": f"Anthropic error: {e.message}", "status": 502})
    except Exception as e:
        yield ("error", {"error": f"AI call failed ({e}).", "status": 503})
    finally:
        if not finished and (started["in"] or produced):
            record_usage(conn, kind, cfg["model"], started["in"], estimate_tokens("x" * produced),
                         started["cread"], started["cwrite"])


def prompt_guard(conn, prompt, max_tokens, confirmed):
    """Refuse a one-shot tool before it costs anything, the way chat_guard does.

    Same two refusals as call_claude, pulled out so a streamed run can be refused with
    an ordinary JSON answer before the stream opens rather than halfway into one.
    """
    cfg = settings(conn)
    in_tokens = estimate_tokens(prompt)
    est = estimate_cost(cfg, in_tokens, max_tokens)
    used = spent_today(conn, cfg)

    if cfg["daily_cap_usd"] and used["usd"] >= cfg["daily_cap_usd"]:
        raise AiRefused({"error": "You have reached today's AI limit.", "reason": "daily_cap",
                         "spentToday": used["usd"], "dailyCap": cfg["daily_cap_usd"]})
    everyone = spent_today_everyone(cfg)
    if GLOBAL_CAP_USD and everyone is not None and everyone >= GLOBAL_CAP_USD:
        raise AiRefused({"error": "Vesta has reached today's AI limit across all accounts.",
                         "reason": "global_cap", "spentTodayEveryone": everyone,
                         "globalCap": GLOBAL_CAP_USD})
    if not confirmed and cfg["confirm_over_usd"] and est > cfg["confirm_over_usd"]:
        raise AiRefused({"error": "This is a big one.", "reason": "confirm",
                         "estimateUsd": round(est, 4), "inputTokens": in_tokens,
                         "maxOutputTokens": max_tokens, "spentToday": used["usd"],
                         "dailyCap": cfg["daily_cap_usd"]}, status=409)
    return cfg, in_tokens


def stream_claude(conn, cfg, kind, prompt, max_tokens=4000):
    """One prompt, streamed: yields ("text", s), then ("done", usage) or ("error", p).

    The same shape as stream_claude_chat so the page can read either with one reader.
    A one-shot tool has no conversation and no cached prefix, so this sends a single
    user message and nothing else. Closing it part way still records what was billed.
    """
    started = {"in": 0}
    produced = 0
    finished = False
    try:
        with anthropic.Anthropic().messages.stream(
                model=cfg["model"], max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]) as stream:
            for ev in stream:
                if ev.type == "message_start":
                    started["in"] = getattr(ev.message.usage, "input_tokens", 0) or 0
                elif ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                    produced += len(ev.delta.text)
                    yield ("text", ev.delta.text)
            final = stream.get_final_message()
        tin, tout, cread, cwrite = usage_from(final, started["in"], estimate_tokens("x" * produced))
        record_usage(conn, kind, cfg["model"], tin, tout, cread, cwrite)
        finished = True
        yield ("done", {"inputTokens": tin, "outputTokens": tout, "model": cfg["model"],
                        "stopReason": final.stop_reason})
    except anthropic.AuthenticationError:
        yield ("error", {"error": "The Anthropic API key is missing or was rejected.", "status": 503})
    except anthropic.RateLimitError:
        yield ("error", {"error": "Rate limited by Anthropic. Wait a moment and try again.", "status": 429})
    except anthropic.APIConnectionError:
        yield ("error", {"error": "Lost the connection to the Anthropic API.", "status": 502})
    except anthropic.APIStatusError as e:
        yield ("error", {"error": f"Anthropic error: {e.message}", "status": 502})
    except Exception as e:
        yield ("error", {"error": f"AI call failed ({e}).", "status": 503})
    finally:
        if not finished and (started["in"] or produced):
            record_usage(conn, kind, cfg["model"], started["in"],
                         estimate_tokens("x" * produced), 0, 0)


def usage_from(response, fallback_in, fallback_out):
    """The four token counts off a response, whatever the SDK hands back."""
    u = getattr(response, "usage", None)
    if not u:
        return fallback_in, fallback_out, 0, 0
    return (getattr(u, "input_tokens", fallback_in) or 0,
            getattr(u, "output_tokens", fallback_out) or 0,
            getattr(u, "cache_read_input_tokens", 0) or 0,
            getattr(u, "cache_creation_input_tokens", 0) or 0)


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
    state = assignment_state(conn, it)
    if state:
        bits.append("Where the student is with it: " + state)
    return "\n".join(bits), it, cls


def assignment_state(conn, it):
    """How far along this assignment already is, in one line for the prompt.

    Without it every generation treats the work as untouched, so an outline asked for
    on day six arrives identical to the one from day one and ignores the 900 words
    already written. Days left, time logged and work already generated are all things
    Vesta knows; saying them changes the answer.
    """
    bits = []
    left = days_until(it["due_date"])
    if left is not None:
        bits.append("due today" if left == 0
                    else (f"{-left} day(s) overdue" if left < 0 else f"{left} day(s) left"))
    secs = (it["focus_seconds"] or 0) if "focus_seconds" in it.keys() else 0
    if secs >= 300:
        bits.append(f"{round(secs / 60)} minutes of focused work already logged on it")

    made = conn.execute(
        "SELECT COUNT(*) AS n FROM headstarts WHERE item_id=?", (it["id"],)).fetchone()["n"]
    turns = conn.execute(
        "SELECT COUNT(*) AS n FROM thread_messages m JOIN threads t ON t.id=m.thread_id"
        " WHERE t.item_id=? AND m.role='assistant'", (it["id"],)).fetchone()["n"]
    if made or turns:
        bits.append(f"{made + turns} piece(s) of Vesta output already exist for it, so "
                    "build on that rather than starting over")

    decks = conn.execute(
        "SELECT COUNT(*) AS n FROM flashcard_decks WHERE item_id=?", (it["id"],)).fetchone()["n"]
    if decks:
        bits.append(f"{decks} set(s) of flashcards already made for it")
    return "; ".join(bits)


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


_VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv", "flv"}
_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "heic", "tif", "tiff"}
_ARCHIVE_EXTS = {"zip", "rar", "7z", "tar", "gz", "tgz"}


def unreadable_reason(m):
    """Why a file has no text a tool could read, in one word the picker can explain.

    The picker used to say "no readable text" about everything from a lecture video to
    a 20 MB deck whose text simply had not been read yet, which told him nothing about
    which ones were worth waiting for.
    """
    name = (m["filename"] or m["title"] or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    mime = (m["mimetype"] or "").lower()
    if mime.startswith("video/") or ext in _VIDEO_EXTS:
        return "video"
    if mime.startswith("image/") or ext in _IMAGE_EXTS:
        return "image"
    if ext in _ARCHIVE_EXTS:
        return "archive"
    key = m["import_key"] if "import_key" in m.keys() else None
    if m["kind"] != "file" and (key or "").startswith("canvas:file:"):
        return "reading"          # a Canvas file whose text is read in the background
    if m["kind"] != "file":
        return "link"
    return "none"                 # a scanned PDF, or a format with no reader


@bp.route("/api/ai/context/<cid>")
def ai_context(cid):
    """Everything in a class that could be fed to a tool, so the user can pick."""
    conn = get_db()
    # With the folder each file sits in, its size and type, so the picker can group a
    # class's files the way its Files tab does rather than listing fifty names at once.
    mats = conn.execute(
        "SELECT m.id AS id, m.title AS title, m.filename AS filename, m.category AS category, "
        "  m.kind AS kind, m.mimetype AS mimetype, m.size AS size, m.import_key AS import_key, "
        "  m.folder_id AS folder_id, f.name AS folder_name, "
        "  CASE WHEN m.extracted_text IS NULL OR m.extracted_text='' THEN 0 ELSE 1 END AS readable, "
        "  LENGTH(COALESCE(m.extracted_text,'')) AS chars "
        "FROM materials m LEFT JOIN file_folders f ON f.id = m.folder_id "
        "WHERE m.class_id=? ORDER BY m.created_at DESC", (cid,)).fetchall()
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
                   "filename": m["filename"], "category": m["category"],
                   "readable": bool(m["readable"]), "chars": m["chars"], "size": m["size"],
                   "folderId": m["folder_id"], "folderName": m["folder_name"],
                   "reason": None if m["readable"] else unreadable_reason(m)} for m in mats],
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


# The five tools whose output still lives on the assignment, under the names the rest
# of the app already reads. The other five are transient, and always were.
LEGACY_KINDS = {"outline": "essay_outline", "explain": "explain", "draft": "draft",
                "study_plan": "study_outline", "summarize": "synthesis"}


def save_tool_output(conn, item, tool_key, content, used):
    """Keep a tool's output on its assignment, if it is one of the five that belong there."""
    if not item or tool_key not in LEGACY_KINDS:
        return None
    kind = LEGACY_KINDS[tool_key]
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
    conn.execute("DELETE FROM headstart_sources WHERE headstart_id=?", (saved_id,))
    for src in used:
        conn.execute(
            "INSERT INTO headstart_sources (id, headstart_id, material_id, note_id, created_at)"
            " VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), saved_id,
             src["id"] if src["type"] == "file" else None,
             src["id"] if src["type"] == "note" else None, now))
    conn.commit()
    return saved_id


@bp.route("/api/ai/run/stream", methods=["POST"])
def ai_run_stream():
    """The same tools, written out as they are generated.

    Threads and the Humanizer have streamed since 2026-09-19; the one-shot tools were
    the last place left showing nothing for thirty seconds, which reads as broken
    however good the answer is when it lands. A refusal is still an ordinary JSON
    answer, decided before the stream opens, so the cost gate still works.
    """
    data = request.get_json(force=True) or {}
    tool_key = data.get("tool")
    if tool_key not in TOOLS:
        return jsonify({"error": "Unknown tool."}), 400

    conn = get_db()
    handed_off = False
    try:
        brief, item, cls = assignment_brief(conn, data.get("itemId"))
        class_id = data.get("classId") or (item["class_id"] if item else None)
        context, used = collect_sources(conn, data.get("selection"), class_id, data.get("itemId"))
        user_text = (data.get("text") or "").strip()
        if tool_key in ("revise", "refine") and not user_text:
            return jsonify({"error": "Paste the writing you want me to work on first."}), 400

        prompt = build_tool_prompt(tool_key, brief, context, user_text, data.get("instructions"))
        max_tokens = TOOLS[tool_key]["max_tokens"]
        try:
            cfg, _ = prompt_guard(conn, prompt, max_tokens, bool(data.get("confirmed")))
        except AiRefused as e:
            return jsonify(e.payload), e.status

        def events():
            parts, error = [], None
            inner = stream_claude(conn, cfg, tool_key, prompt, max_tokens)
            try:
                try:
                    yield sse_event("start", {"sources": used})
                    for kind, payload in inner:
                        if kind == "text":
                            parts.append(payload)
                            yield sse_event("text", {"t": payload})
                        elif kind == "error":
                            error = payload
                finally:
                    # Closed first, so its usage record lands while the connection is open.
                    inner.close()
                    text = "".join(parts).strip()
                    saved_id = save_tool_output(conn, item, tool_key, text, used) if text else None
                if error:
                    yield sse_event("error", error)
                yield sse_event("done", {"content": "".join(parts).strip(), "sources": used,
                                         "savedId": saved_id,
                                         "usage": spent_today(conn, settings(conn))})
            finally:
                conn.close()

        handed_off = True
        return Response(stream_with_context(events()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except AiRefused as e:
        return jsonify(e.payload), e.status
    finally:
        if not handed_off:
            conn.close()


def sse_event(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


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
        did = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO flashcard_decks (id, semester_id, class_id, item_id, name, description,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (did, semester_for(conn, class_id), class_id, data.get("itemId") or None,
             data.get("name") or "Generated deck",
             "From " + ", ".join(s["title"] for s in used[:3]) if used else "", now, now))
        for i, c in enumerate(cards):
            conn.execute(
                "INSERT INTO flashcards (id, deck_id, front, back, kind, due_date, sort_order, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), did, c.get("front") or "", c.get("back") or "",
                 c.get("kind") if c.get("kind") in ("term", "question", "concept") else "term",
                 None, i, now))
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


def days_until(iso):
    """Whole days from today to an ISO date, or None. Negative means it has passed."""
    if not iso:
        return None
    try:
        then = datetime.strptime(iso[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return (then - datetime.strptime(today_str(), "%Y-%m-%d")).days


# A card that has been forgotten this many times is stuck rather than merely hard, and
# is worth showing the student by name instead of leaving in the rotation.
STUCK_LAPSES = 3


# Easy is a bigger step than Good, not just a better ease factor. Plain SM-2 gives
# both the same interval and only diverges at the review after next, which means the
# two buttons predict the same date on the card in front of you and the choice looks
# like it does not matter. These are Anki's steps and its easy bonus, which is the
# same algorithm with the difference made visible today.
FIRST_STEP = {"good": 1, "easy": 4}
SECOND_STEP = {"good": 6, "easy": 10}
EASY_BONUS = 1.3


def sm2_next(ease, reps, interval, lapses, grade):
    """The next sighting of a card, given how the last one went.

    SM-2, the algorithm behind Anki and SuperMemo. 0-2 is a miss and sends the card
    back to the bottom of the ladder; 3-5 is a hit and pushes the next sighting
    further out, scaled by how easy the card has proven to be.

    `fcPredictDays` in static/index.html mirrors this so the buttons can say what
    each grade costs before it is pressed. Change one and change the other.
    """
    ease = ease if ease else 2.5
    reps = reps or 0
    interval = interval or 0
    lapses = lapses or 0
    if grade < 3:
        reps, interval, lapses = 0, 1, lapses + 1
    else:
        easy = grade >= 5
        reps += 1
        if reps == 1:
            interval = FIRST_STEP["easy" if easy else "good"]
        elif reps == 2:
            interval = SECOND_STEP["easy" if easy else "good"]
        else:
            interval = max(1, round(interval * ease * (EASY_BONUS if easy else 1.0)))
        ease = max(1.3, ease + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)))
    return {"ease": round(ease, 3), "interval": interval, "repetitions": reps,
            "lapses": lapses}


def apply_review(conn, cid, grade, deck_id=None):
    """Record one grade against one card. Returns the updated row, or None."""
    grade = max(0, min(5, int(grade)))
    if deck_id:
        c = conn.execute("SELECT * FROM flashcards WHERE id=? AND deck_id=?",
                         (cid, deck_id)).fetchone()
    else:
        c = conn.execute("SELECT * FROM flashcards WHERE id=?", (cid,)).fetchone()
    if not c:
        return None
    nxt = sm2_next(c["ease"], c["repetitions"], c["interval_days"], c["lapses"], grade)
    now = datetime.utcnow()
    due = (now + timedelta(days=nxt["interval"])).strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE flashcards SET ease=?, interval_days=?, repetitions=?, lapses=?, due_date=?,"
        " last_reviewed_at=? WHERE id=?",
        (nxt["ease"], nxt["interval"], nxt["repetitions"], nxt["lapses"], due,
         now.isoformat(), cid))
    return conn.execute("SELECT * FROM flashcards WHERE id=?", (cid,)).fetchone()


def card_json(r):
    return {"id": r["id"], "deckId": r["deck_id"], "front": r["front"], "back": r["back"],
            "kind": r["kind"], "ease": r["ease"], "interval": r["interval_days"],
            "repetitions": r["repetitions"], "lapses": r["lapses"], "due": r["due_date"],
            "lastReviewed": r["last_reviewed_at"], "suspended": bool(r["suspended"]),
            "learnLevel": (r["learn_level"] or 0) if "learn_level" in r.keys() else 0,
            "sortOrder": r["sort_order"]}


@bp.route("/api/decks", methods=["GET", "POST"])
def decks():
    conn = get_db()
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        now = datetime.utcnow().isoformat()
        did = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO flashcard_decks (id, semester_id, class_id, item_id, name, description,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (did, semester_for(conn, data.get("classId")), data.get("classId"),
             data.get("itemId") or None,
             (data.get("name") or "New deck").strip(),
             data.get("description") or "", now, now))
        conn.commit()
        conn.close()
        return jsonify({"id": did}), 201

    where, args = " WHERE d.semester_id=?", [active_semester_id(conn)]
    if request.args.get("classId"):
        where, args = " WHERE d.class_id=?", [request.args["classId"]]
    today = today_str()
    # The counts a study decision is actually made from: what is due, what has never
    # been seen, what is learned, what keeps being forgotten, and the next exam in the
    # same course. Without that last one a set says "nothing due" three days before a
    # midterm, which is true and useless.
    rows = conn.execute(
        "SELECT d.*, "
        " (SELECT COUNT(*) FROM flashcards c WHERE c.deck_id=d.id) AS n_cards, "
        " (SELECT COUNT(*) FROM flashcards c WHERE c.deck_id=d.id AND c.suspended=0"
        "   AND (c.due_date IS NULL OR c.due_date<=?)) AS n_due, "
        " (SELECT COUNT(*) FROM flashcards c WHERE c.deck_id=d.id AND c.repetitions=0) AS n_new, "
        " (SELECT COUNT(*) FROM flashcards c WHERE c.deck_id=d.id"
        "   AND COALESCE(c.learn_level,0)>=2) AS n_learned, "
        " (SELECT COUNT(*) FROM flashcards c WHERE c.deck_id=d.id"
        "   AND COALESCE(c.lapses,0)>=" + str(STUCK_LAPSES) + ") AS n_stuck, "
        " (SELECT MAX(c.last_reviewed_at) FROM flashcards c WHERE c.deck_id=d.id) AS seen_at, "
        " (SELECT i.due_date FROM items i WHERE i.class_id=d.class_id"
        "   AND COALESCE(i.status,'')<>'done' AND i.type IN ('exam','quiz')"
        "   AND i.due_date IS NOT NULL AND i.due_date<>'' AND i.due_date>=?"
        "   ORDER BY i.due_date LIMIT 1) AS exam_date, "
        " (SELECT i.title FROM items i WHERE i.class_id=d.class_id"
        "   AND COALESCE(i.status,'')<>'done' AND i.type IN ('exam','quiz')"
        "   AND i.due_date IS NOT NULL AND i.due_date<>'' AND i.due_date>=?"
        "   ORDER BY i.due_date LIMIT 1) AS exam_title "
        "FROM flashcard_decks d" + where + " ORDER BY d.updated_at DESC",
        [today, today, today] + args).fetchall()
    conn.close()
    return jsonify([{"id": r["id"], "classId": r["class_id"],
                     "itemId": r["item_id"] if "item_id" in r.keys() else None,
                     "name": r["name"],
                     "description": r["description"], "cardCount": r["n_cards"],
                     "dueCount": r["n_due"], "newCount": r["n_new"],
                     "learnedCount": r["n_learned"], "stuckCount": r["n_stuck"],
                     "lastReviewed": r["seen_at"],
                     "examDate": r["exam_date"], "examTitle": r["exam_title"],
                     "examDays": days_until(r["exam_date"]),
                     "createdAt": r["created_at"], "updatedAt": r["updated_at"]} for r in rows])


@bp.route("/api/decks/<did>/learn", methods=["POST"])
def save_learn_progress(did):
    """How far Learn has got with each card in this set.

    Sent at the end of each round rather than after every answer: a round is a handful
    of cards, and one request for it keeps a slow connection from making the mode feel
    the way the rest of the app used to. Levels are clamped here, so a hand-written
    request cannot mark a set learned.
    """
    data = request.get_json(force=True) or {}
    levels = data.get("levels") or {}
    conn = get_db()
    if not conn.execute("SELECT 1 FROM flashcard_decks WHERE id=?", (did,)).fetchone():
        conn.close()
        abort(404)
    for cid, lvl in list(levels.items())[:500]:
        try:
            lvl = max(0, min(2, int(lvl)))
        except (TypeError, ValueError):
            continue
        conn.execute("UPDATE flashcards SET learn_level=? WHERE id=? AND deck_id=?",
                     (lvl, cid, did))
    conn.commit()
    rows = conn.execute("SELECT id, learn_level FROM flashcards WHERE deck_id=?", (did,)).fetchall()
    conn.close()
    return jsonify({"levels": {r["id"]: (r["learn_level"] or 0) for r in rows}})


@bp.route("/api/decks/<did>/learn/reset", methods=["POST"])
def reset_learn_progress(did):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM flashcard_decks WHERE id=?", (did,)).fetchone():
        conn.close()
        abort(404)
    conn.execute("UPDATE flashcards SET learn_level=0 WHERE deck_id=?", (did,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


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
        # No due date means due now, which is what every query here already assumes.
        # Stamping today's UTC date instead made a card written on a Sunday evening in
        # Vancouver unreviewable until Monday, since the browser counts days locally.
        (cid, did, data.get("front") or "", data.get("back") or "",
         data.get("kind") if data.get("kind") in ("term", "question", "concept") else "term",
         None, nxt, now))
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
    try:
        grade = int((request.get_json(force=True) or {}).get("grade", 3))
    except (TypeError, ValueError):
        grade = 3
    row = apply_review(conn, cid, grade)
    if row is None:
        conn.close()
        abort(404)
    conn.commit()
    conn.close()
    return jsonify(card_json(row))


@bp.route("/api/cards/review-batch", methods=["POST"])
def review_cards_batch():
    """Several grades at once, so Learn and Test can feed the same schedule.

    Learn and Test used to be sealed off from scheduling: getting a term wrong in a
    test told Vesta nothing, so the card stayed months out while the student plainly
    did not know it. Both now send what happened at the end of a round, in one
    request, and a miss brings the card back tomorrow whichever mode found it.
    """
    data = request.get_json(force=True) or {}
    reviews = data.get("reviews") or []
    if not isinstance(reviews, list):
        abort(400)
    conn = get_db()
    out = []
    for r in reviews[:500]:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        try:
            grade = int(r.get("grade", 3))
        except (TypeError, ValueError):
            continue
        row = apply_review(conn, r["id"], grade)
        if row is not None:
            out.append(card_json(row))
    conn.commit()
    conn.close()
    return jsonify({"cards": out})


# ---------------------------------------------------------------------------
# One queue across every set
#
# Cards are due per set, so a student with five sets had to remember to open five
# things. Scope decides what goes in the queue rather than which set it came from:
#
#   due      everything ready for review right now, oldest first, new cards after
#   cram     everything in scope regardless of schedule, least known first
#   trouble  only the cards that keep being forgotten
#
# Every card carries the name of the set it came from, since a mixed queue otherwise
# gives no clue which course you are being asked about.
# ---------------------------------------------------------------------------
QUEUE_LIMIT = 400


@bp.route("/api/study/queue")
def study_queue():
    scope = request.args.get("scope") or "due"
    class_id = request.args.get("classId")
    deck_id = request.args.get("deckId")
    conn = get_db()

    where = ["c.suspended=0"]
    args = []
    if deck_id:
        where.append("c.deck_id=?")
        args.append(deck_id)
    elif class_id:
        where.append("d.class_id=?")
        args.append(class_id)
    else:
        where.append("d.semester_id=?")
        args.append(active_semester_id(conn))

    if scope == "due":
        where.append("(c.due_date IS NULL OR c.due_date<=?)")
        args.append(today_str())
        # Overdue first, then the ones that have never been seen.
        order = "ORDER BY CASE WHEN c.due_date IS NULL THEN 1 ELSE 0 END, c.due_date, c.sort_order"
    elif scope == "trouble":
        where.append("COALESCE(c.lapses,0)>=?")
        args.append(STUCK_LAPSES)
        order = "ORDER BY c.lapses DESC, c.sort_order"
    else:
        # Cram: least known first, so the twenty minutes before a midterm go on the
        # terms that are not there yet rather than the ones already known cold.
        order = ("ORDER BY COALESCE(c.learn_level,0), COALESCE(c.lapses,0) DESC,"
                 " c.repetitions, c.sort_order")

    rows = conn.execute(
        "SELECT c.*, d.name AS deck_name, d.class_id AS deck_class_id"
        " FROM flashcards c JOIN flashcard_decks d ON d.id=c.deck_id"
        " WHERE " + " AND ".join(where) + " " + order + " LIMIT " + str(QUEUE_LIMIT),
        args).fetchall()
    conn.close()
    cards = []
    for r in rows:
        card = card_json(r)
        card["deckName"] = r["deck_name"]
        card["classId"] = r["deck_class_id"]
        cards.append(card)
    return jsonify({"scope": scope, "cards": cards})

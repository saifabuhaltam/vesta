"""HTTP side of calendar import: which section is yours, what SFU says, applying it.

Deliberately shaped like syllabus import, because the student already knows that flow:
nothing reaches a class until `/apply`, that runs in one transaction, and anything
unticked on the review screen is never written.
"""
import json
import uuid
from datetime import datetime, timedelta

from flask import Blueprint, abort, jsonify, request

import calsync
from db import get_db, active_semester, active_semester_id, semester_for

bp = Blueprint("calendar_api", __name__)


def now():
    return datetime.utcnow().isoformat()


def current_term(conn):
    return (active_semester(conn)["name"] or "")


# ---------------------------------------------------------------------------
# 1. Which section is yours
# ---------------------------------------------------------------------------
@bp.route("/api/calendar/sfu/sections")
def sfu_sections():
    code = (request.args.get("code") or "").strip()
    term = (request.args.get("term") or "").strip()
    conn = get_db()
    if not code and request.args.get("classId"):
        row = conn.execute("SELECT code FROM classes WHERE id=?", (request.args["classId"],)).fetchone()
        code = (row["code"] if row else "") or ""
    term = term or current_term(conn)
    conn.close()
    if not code:
        return jsonify({"error": "Give the course code first."}), 400
    if not term:
        return jsonify({"error": "Set the term in Settings first, so Vesta knows which one to ask for."}), 400
    return jsonify(calsync.section_options(code, term))


# ---------------------------------------------------------------------------
# 2. What SFU says, as a draft to check
# ---------------------------------------------------------------------------
@bp.route("/api/calendar/sfu/draft", methods=["POST"])
def sfu_draft():
    data = request.get_json(force=True) or {}
    sections = [s for s in (data.get("sections") or []) if s]
    if not sections:
        return jsonify({"error": "Pick your section first."}), 400
    conn = get_db()
    class_id = data.get("classId") or None
    code = (data.get("code") or "").strip()
    if class_id and not code:
        row = conn.execute("SELECT code FROM classes WHERE id=?", (class_id,)).fetchone()
        code = (row["code"] if row else "") or ""
    term = (data.get("term") or "").strip() or current_term(conn)

    draft = calsync.schedule_draft(conn, class_id, code, term, sections)
    if draft.get("error"):
        conn.close()
        return jsonify(draft), 400
    if not draft["meetings"] and not draft["items"]:
        conn.close()
        return jsonify({"error": "SFU published nothing usable for that section.",
                        "warnings": draft["warnings"]}), 400

    iid = str(uuid.uuid4())
    # One pending draft per class. Pulling again replaces the last unapplied one rather
    # than stacking another "waiting for you to check it" banner on the Classes page.
    if class_id:
        conn.execute("DELETE FROM calendar_imports WHERE class_id=? AND status='review'", (class_id,))
    conn.execute("INSERT INTO calendar_imports (id, class_id, source, label, draft, status, created_at)"
                 " VALUES (?,?,?,?,?,?,?)",
                 (iid, class_id, "sfu", draft["label"], json.dumps(draft, default=str), "review", now()))
    conn.commit()
    conn.close()
    return jsonify(dict(draft, id=iid)), 201


@bp.route("/api/calendar/imports/<iid>", methods=["GET", "DELETE"])
def one_import(iid):
    conn = get_db()
    row = conn.execute("SELECT * FROM calendar_imports WHERE id=?", (iid,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    if request.method == "DELETE":
        conn.execute("DELETE FROM calendar_imports WHERE id=?", (iid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    out = dict(json.loads(row["draft"]) if row["draft"] else {},
               id=row["id"], status=row["status"], label=row["label"], source=row["source"])
    conn.close()
    return jsonify(out)


# ---------------------------------------------------------------------------
# 3. Apply: exactly what was reviewed, in one transaction
# ---------------------------------------------------------------------------
@bp.route("/api/calendar/imports/<iid>/apply", methods=["POST"])
def apply_import(iid):
    body = request.get_json(force=True) or {}
    draft = body.get("draft") or {}
    conn = get_db()
    row = conn.execute("SELECT * FROM calendar_imports WHERE id=?", (iid,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    if row["status"] == "imported":
        conn.close()
        return jsonify({"error": "This schedule has already been imported."}), 409

    class_id = draft.get("classId") or body.get("classId")
    if not class_id:
        conn.close()
        return jsonify({"error": "Choose which class this schedule belongs to."}), 400

    t = now()
    counts = {"meetings": 0, "updated": 0, "removed": 0, "items": 0}
    try:
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM classes WHERE id=?", (class_id,)).fetchone():
            raise ValueError("that class no longer exists")

        for m in draft.get("meetings", []):
            if not m.get("include"):
                continue
            if m.get("existingId"):
                conn.execute(
                    "UPDATE schedule_entries SET day=?, start=?, \"end\"=?, location=?, kind=?,"
                    " section=?, start_date=?, end_date=? WHERE id=? AND class_id=?",
                    (m["day"], m["start"], m["end"], m.get("location") or "", m["kind"],
                     m.get("section") or "", m.get("startDate"), m.get("endDate"),
                     m["existingId"], class_id))
                counts["updated"] += 1
            else:
                conn.execute(
                    "INSERT INTO schedule_entries (id, class_id, day, start, \"end\", location, kind,"
                    " section, start_date, end_date) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), class_id, m["day"], m["start"], m["end"],
                     m.get("location") or "", m["kind"], m.get("section") or "",
                     m.get("startDate"), m.get("endDate")))
                counts["meetings"] += 1

        for it in draft.get("items", []):
            # an exam that already exists is left alone: its score is the student's
            if not it.get("include") or it.get("existingId"):
                continue
            conn.execute(
                "INSERT INTO items (id, semester_id, class_id, title, type, due_date, due_time, status, notes,"
                " created_at, location, import_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), semester_for(conn, class_id), class_id,
                 it["title"], it["type"], it.get("dueDate"),
                 it.get("dueTime"), "todo", "", t, it.get("location") or "",
                 f"sfu|{it['type']}|{it.get('dueDate') or ''}"))
            counts["items"] += 1

        for gone in draft.get("removed", []):
            if gone.get("include") and gone.get("existingId"):
                conn.execute("DELETE FROM schedule_entries WHERE id=? AND class_id=?",
                             (gone["existingId"], class_id))
                counts["removed"] += 1

        # SFU knows the real term dates; fill them only if nobody has set them yet
        if draft.get("firstDay") and draft.get("lastDay"):
            conn.execute(
                "UPDATE semesters SET start_date=COALESCE(NULLIF(start_date,''),?),"
                " end_date=COALESCE(NULLIF(end_date,''),?) WHERE id=?",
                (draft["firstDay"], draft["lastDay"], active_semester_id(conn)))

        conn.execute("UPDATE calendar_imports SET status='imported', imported_at=?, draft=? WHERE id=?",
                     (t, json.dumps(draft, default=str), iid))
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        conn.close()
        return jsonify({"error": f"Nothing was imported: {e}"}), 400
    conn.close()
    return jsonify({"classId": class_id, "counts": counts}), 201


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------
import os
import secrets

from flask import redirect

import gcal
import gsync

STATE_KEY = "google_oauth"


DEFAULT_REDIRECT = "http://localhost:5000/api/calendar/google/callback"


def redirect_uri():
    """The callback URL, which Google matches exactly.

    Derived from the address actually being browsed rather than hard-coded, because
    Vesta's port is not fixed: macOS Control Center permanently occupies 5000, so it
    usually ends up on 5055 or similar, and a hard-coded 5000 fails with
    redirect_uri_mismatch at the worst moment.

    Only loopback hosts are trusted. Google rejects a raw LAN IP as a redirect target,
    so reaching Vesta from a phone on the wifi falls back to the configured value.
    """
    env = (os.environ.get("GOOGLE_REDIRECT_URI") or "").strip()
    if env:
        return env
    try:
        host = request.host or ""
    except RuntimeError:                  # called outside a request context
        return DEFAULT_REDIRECT
    if not host:
        return DEFAULT_REDIRECT
    if host.split(":")[0] in ("localhost", "127.0.0.1"):
        return f"http://{host}/api/calendar/google/callback"
    # Any other host is a real deployment, and Google requires https for those. The
    # scheme is read from the proxy header because Railway terminates TLS in front of
    # the app, so request.scheme alone says "http" and would produce a callback Google
    # rejects. Falling back to localhost here, as this used to, meant the deployed app
    # confidently sent Google a URL that could never match.
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    scheme = "https" if proto in ("", "https") else proto
    return f"{scheme}://{host}/api/calendar/google/callback"


def put_state(conn, value):
    """The OAuth state lives in the database because this app has no sessions.

    There is no secret_key and no cookies, so there is nowhere in a request to keep a
    CSRF token between the redirect out and the callback back.
    """
    blob = json.dumps({"state": value, "at": now()})
    if not conn.execute("UPDATE app_settings SET value=? WHERE key=?",
                        (blob, STATE_KEY)).rowcount:
        conn.execute("INSERT INTO app_settings (key, value) VALUES (?,?)", (STATE_KEY, blob))
    conn.commit()


def take_state(conn):
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (STATE_KEY,)).fetchone()
    conn.execute("DELETE FROM app_settings WHERE key=?", (STATE_KEY,))
    conn.commit()
    try:
        return (json.loads(row["value"]) or {}).get("state") if row and row["value"] else None
    except Exception:
        return None


def access_token(conn, acct):
    """A usable access token, refreshing when the stored one has expired."""
    if acct["access_token"] and acct["token_expires"] and now() < acct["token_expires"]:
        return acct["access_token"]
    if not acct["refresh_token"]:
        raise gcal.GoogleError("Vesta has no refresh token for Google. Reconnect it.")
    tok = gcal.refresh_token(acct["refresh_token"])
    expires = (datetime.utcnow() + timedelta(seconds=int(tok.get("expires_in") or 3600) - 60)).isoformat()
    conn.execute("UPDATE calendar_accounts SET access_token=?, token_expires=?, last_error=NULL WHERE id=?",
                 (tok.get("access_token"), expires, acct["id"]))
    conn.commit()
    return tok.get("access_token")


@bp.route("/api/calendar/google/status")
def google_status():
    conn = get_db()
    acct = gsync.account(conn)
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM calendar_imports WHERE source='google' AND status='review'"
    ).fetchone()["n"]
    conflicts = 0
    if acct:
        conflicts = conn.execute(
            "SELECT COUNT(*) AS n FROM sync_links WHERE account_id=? AND state='conflict'",
            (acct["id"],)).fetchone()["n"]
    out = {
        "configured": gcal.configured(),
        "connected": bool(acct),
        "name": acct["name"] if acct else None,
        "lastSync": acct["last_sync"] if acct else None,
        "lastError": acct["last_error"] if acct else None,
        "conflicts": conflicts,
        "pendingReview": pending,
        "redirectUri": redirect_uri(),
    }
    conn.close()
    return jsonify(out)


@bp.route("/api/calendar/google/start")
def google_start():
    if not gcal.configured():
        return jsonify({"error": "Put GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first, "
                                 "then restart Vesta."}), 400
    conn = get_db()
    state = secrets.token_urlsafe(24)
    put_state(conn, state)
    conn.close()
    return jsonify({"url": gcal.auth_url(redirect_uri(), state)})


@bp.route("/api/calendar/google/callback")
def google_callback():
    """Google sends the student back here. Always land them in the app, never on JSON."""
    conn = get_db()
    expected = take_state(conn)
    given = request.args.get("state")
    if request.args.get("error"):
        conn.close()
        return redirect("/?google=denied")
    if not expected or given != expected:
        conn.close()
        return redirect("/?google=badstate")
    try:
        tok = gcal.exchange_code(request.args.get("code") or "", redirect_uri())
        client = gcal.Client(tok.get("access_token"))
        cal_id = client.ensure_calendar()
    except gcal.GoogleError as e:
        conn.close()
        return redirect("/?google=failed")

    expires = (datetime.utcnow() + timedelta(seconds=int(tok.get("expires_in") or 3600) - 60)).isoformat()
    old = gsync.account(conn)
    if old:
        conn.execute("DELETE FROM calendar_accounts WHERE id=?", (old["id"],))
    conn.execute(
        "INSERT INTO calendar_accounts (id, provider, name, calendar_id, refresh_token,"
        " access_token, token_expires, direction, enabled, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,1,?)",
        (str(uuid.uuid4()), "google", gcal.CALENDAR_NAME, cal_id,
         # Google only sends a refresh token on the first consent; keep the old one if
         # this was a re-authorisation that did not include one.
         tok.get("refresh_token") or (old["refresh_token"] if old else None),
         tok.get("access_token"), expires, "both", now()))
    conn.commit()
    conn.close()
    return redirect("/?google=connected")


@bp.route("/api/calendar/google", methods=["DELETE"])
def google_disconnect():
    """Forget the account here. Events already in Google are left alone on purpose:
    deleting a student's calendar entries because they unlinked an app is not ours to do."""
    conn = get_db()
    acct = gsync.account(conn)
    if acct:
        conn.execute("DELETE FROM calendar_accounts WHERE id=?", (acct["id"],))
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


def _pull(client, cal_id, sync_token):
    """Every page of changes, and the token to resume from next time."""
    events, page, token = [], None, sync_token
    for _ in range(40):                   # ~10k events; a cap beats an unbounded loop
        data = client.list_events(cal_id, sync_token=token, page_token=page)
        events.extend(data.get("items") or [])
        page = data.get("nextPageToken")
        if not page:
            return events, data.get("nextSyncToken")
    return events, None                   # no token: next sync does a full pass


@bp.route("/api/calendar/google/sync", methods=["POST"])
def google_sync():
    conn = get_db()
    acct = gsync.account(conn)
    if not acct:
        conn.close()
        return jsonify({"error": "Connect Google Calendar first."}), 400
    try:
        token = access_token(conn, acct)
        client = gcal.Client(token)
        cal_id = acct["calendar_id"] or client.ensure_calendar()

        # Pull first: a two-sided change has to be known before anything is pushed,
        # or Vesta would overwrite a change it had not noticed yet.
        try:
            raw, next_token = _pull(client, cal_id, acct["sync_token"])
        except gcal.GoogleError as e:
            if not e.resync:
                raise
            conn.execute("UPDATE sync_links SET remote_hash='' WHERE account_id=?", (acct["id"],))
            raw, next_token = _pull(client, cal_id, None)

        pulled = gsync.plan_pull(conn, acct["id"], raw)
        review = []
        for p in pulled:
            if p["action"] in ("conflict", "remote_ahead"):
                gsync.mark(conn, acct["id"], p["externalId"], p["action"])
            elif p["action"] == "deleted_remotely" and p.get("localId"):
                gsync.forget(conn, acct["id"], "item", p["localId"])
            elif p["action"] == "review":
                review.append(p["event"])

        # Anything Google has that Vesta did not create is offered, never written.
        if review:
            conn.execute(
                "INSERT INTO calendar_imports (id, source, label, draft, status, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), "google", f"{len(review)} event(s) from Google Calendar",
                 json.dumps({"source": "google", "events": review}, default=str), "review", now()))

        pushed = gsync.plan_push(conn, acct["id"])
        for p in pushed:
            if p["action"] == "create":
                made = client.insert(cal_id, p["body"])
                gsync.record(conn, acct["id"], "item", p["localId"], made.get("id"),
                             p["localHash"], gcal.remote_hash(made), made.get("etag", ""))
            elif p["action"] == "update":
                made = client.patch(cal_id, p["externalId"], p["body"])
                gsync.record(conn, acct["id"], "item", p["localId"], p["externalId"],
                             p["localHash"], gcal.remote_hash(made), made.get("etag", ""))
            elif p["action"] == "remove":
                try:
                    client.delete(cal_id, p["externalId"])
                except gcal.GoogleError:
                    pass                      # already gone on Google's side is fine
                gsync.forget(conn, acct["id"], "item", p["localId"])

        conn.execute("UPDATE calendar_accounts SET sync_token=?, calendar_id=?, last_sync=?,"
                     " last_error=NULL WHERE id=?",
                     (next_token, cal_id, now(), acct["id"]))
        conn.commit()
    except gcal.GoogleError as e:
        conn.execute("UPDATE calendar_accounts SET last_error=? WHERE id=?", (e.message, acct["id"]))
        conn.commit()
        conn.close()
        return jsonify({"error": e.message}), 502
    counts = gsync.summarise(pushed)
    counts.update({"fromGoogle": gsync.summarise(pulled)})
    conn.close()
    return jsonify({"ok": True, "counts": counts, "needsReview": len(review)})

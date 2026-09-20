"""HTTP side of calendar import: which section is yours, what SFU says, applying it.

Deliberately shaped like syllabus import, because the student already knows that flow:
nothing reaches a class until `/apply`, that runs in one transaction, and anything
unticked on the review screen is never written.
"""
import json
import threading
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


@bp.route("/api/calendar/google/calendars", methods=["GET", "PUT"])
def google_calendars():
    """The student's Google calendars, and which of them Vesta reads.

    GET asks Google every time rather than trusting what is stored. Calendars are added,
    renamed and unsubscribed outside Vesta, and a picker showing last week's list is
    worse than no picker.
    """
    conn = get_db()
    acct = gsync.account(conn)
    if not acct:
        conn.close()
        return jsonify({"error": "Connect Google Calendar first."}), 400

    if request.method == "PUT":
        chosen = (request.get_json(force=True) or {}).get("calendarIds") or []
        rows = gsync.set_enabled(conn, acct["id"], chosen)
        out = [_feed_json(r) for r in rows]
        conn.close()
        return jsonify(out)

    try:
        client = gcal.Client(access_token(conn, acct))
        rows = gsync.merge_feeds(conn, acct["id"], client.list_calendars())
    except gcal.GoogleError as e:
        conn.close()
        return jsonify({"error": e.message}), 502
    out = [_feed_json(r) for r in rows]
    conn.close()
    return jsonify(out)


def _feed_json(r):
    return {
        "calendarId": r["calendar_id"],
        "name": r["name"] or r["calendar_id"],
        "colour": r["colour"] or "",
        "writable": bool(r["writable"]),
        "isVesta": bool(r["is_vesta"]),
        "enabled": bool(r["enabled"]),
        "lastSync": r["last_sync"] or "",
        "lastError": r["last_error"] or "",
    }


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


# ---------------------------------------------------------------------------
# Push: Google calls us the moment something changes
# ---------------------------------------------------------------------------
#
# Polling covered most of it -- the page syncs when it opens, when the tab is looked
# at again, four seconds after a local change and on a three-minute timer -- but a
# change made in Google while Vesta is closed waits until Vesta is next opened, and
# one made while the tab sits idle waits up to three minutes. A watch channel removes
# both waits.
#
# Traps, all of them learned the hard way or written down before they bit:
#   - notifications are headers only. The body is empty and X-Goog-Resource-State says
#     only that something moved, so the only sane response is the same incremental
#     sync the Sync button runs.
#   - the first notification after registering is a `sync` ping and means nothing.
#   - the endpoint is public and unauthenticated by nature. It looks up the channel id
#     and trusts nothing else in the request.
#   - Google retries hard on any non-2xx, so it answers 200 at once and works after.
#   - channels last about a week and Google never renews them. A missed renewal looks
#     exactly like the lag this exists to remove, which is why /health reports it.

CHANNEL_TTL_SECONDS = 7 * 24 * 3600
# Renew once a channel is within a day of expiring, which every sync has a chance to do.
RENEW_WITHIN_SECONDS = 24 * 3600


def webhook_address():
    """The HTTPS URL Google will call. Google will not deliver to localhost."""
    env = (os.environ.get("GOOGLE_WEBHOOK_URL") or "").strip()
    if env:
        return env
    try:
        host = request.host or ""
    except RuntimeError:
        return ""
    if not host or host.split(":")[0] in ("localhost", "127.0.0.1"):
        return ""                      # local development: polling only, and that is fine
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    scheme = "https" if proto in ("", "https") else proto
    return f"{scheme}://{host}/api/calendar/google/webhook"


def watched_calendar_ids(conn, acct, vesta_cal_id):
    """Everything worth a channel: the calendars chosen, plus Vesta's own."""
    ids = [f["calendar_id"] for f in gsync.feeds(conn, acct["id"], enabled_only=True)]
    if vesta_cal_id and vesta_cal_id not in ids:
        ids.append(vesta_cal_id)
    return ids


def ensure_channels(conn, client, acct, vesta_cal_id, address=None):
    """Register or renew a watch channel per calendar. Never raises: push is an
    improvement on polling, and failing to register one must not break a sync."""
    address = address if address is not None else webhook_address()
    if not address:
        return {"registered": 0, "renewed": 0, "skipped": "no public address"}
    out = {"registered": 0, "renewed": 0, "failed": 0}
    existing = {r["calendar_id"]: r for r in conn.execute(
        "SELECT * FROM calendar_channels WHERE account_id=?", (acct["id"],)).fetchall()}
    cutoff = (datetime.utcnow() + timedelta(seconds=RENEW_WITHIN_SECONDS)).isoformat()
    for cal_id in watched_calendar_ids(conn, acct, vesta_cal_id):
        have = existing.get(cal_id)
        if have and (have["expiration"] or "") > cutoff:
            continue
        channel_id = str(uuid.uuid4())
        try:
            res = client.watch(cal_id, channel_id, address, ttl_seconds=CHANNEL_TTL_SECONDS)
        except gcal.GoogleError:
            out["failed"] += 1
            continue
        # Google answers with milliseconds since the epoch.
        exp = res.get("expiration")
        try:
            expiry = datetime.utcfromtimestamp(int(exp) / 1000).isoformat() if exp else ""
        except (TypeError, ValueError):
            expiry = ""
        if have:
            try:
                client.stop_channel(have["channel_id"], have["resource_id"])
            except gcal.GoogleError:
                pass                    # the old one expires on its own soon enough
            conn.execute(
                "UPDATE calendar_channels SET channel_id=?, resource_id=?, expiration=?,"
                " created_at=? WHERE id=?",
                (channel_id, res.get("resourceId"), expiry, now(), have["id"]))
            out["renewed"] += 1
        else:
            conn.execute(
                "INSERT INTO calendar_channels (id, account_id, calendar_id, channel_id,"
                " resource_id, expiration, created_at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), acct["id"], cal_id, channel_id,
                 res.get("resourceId"), expiry, now()))
            out["registered"] += 1
        conn.commit()
    return out


@bp.route("/api/calendar/google/webhook", methods=["POST"])
def google_webhook():
    """Google says something changed on a calendar. Answer at once, then sync.

    Deliberately does nothing with the request but read the channel id: this URL is
    public, and anything else in it is unverified.
    """
    channel_id = request.headers.get("X-Goog-Channel-ID") or ""
    state = request.headers.get("X-Goog-Resource-State") or ""
    if not channel_id or state == "sync":
        # The first notification after registering is a handshake and means nothing.
        return ("", 200)
    import db as _db
    conn = _db.get_db(user_id=None)
    owner = None
    try:
        conn.as_owner()                 # no session here: the channel row says whose it is
    except Exception:
        pass                            # SQLite has one account and no owner switch
    try:
        row = conn.execute(
            "SELECT * FROM calendar_channels WHERE channel_id=?", (channel_id,)).fetchone()
        if row is not None:
            owner = row["user_id"] if "user_id" in row.keys() else None
    finally:
        conn.close()
    if row is None:
        return ("", 200)                # an unknown or stale channel: nothing to do
    threading.Thread(target=_sync_for_push, args=(owner,), daemon=True).start()
    return ("", 200)


def _sync_for_push(user_id):
    """The same incremental sync the button runs, without a request to hang it on."""
    import db as _db
    conn = _db.get_db(user_id=user_id)
    try:
        acct = gsync.account(conn)
        if not acct:
            return
        token = access_token(conn, acct)
        client = gcal.Client(token)
        cal_id = acct["calendar_id"] or client.ensure_calendar()
        run_sync(conn, acct, client, cal_id, register_channels=False)
    except Exception:
        # A push that fails is a lag, not a loss: the next poll picks it up.
        pass
    finally:
        conn.close()


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
        result = run_sync(conn, acct, client, cal_id, address=webhook_address())
    except gcal.GoogleError as e:
        conn.execute("UPDATE calendar_accounts SET last_error=? WHERE id=?", (e.message, acct["id"]))
        conn.commit()
        conn.close()
        return jsonify({"error": e.message}), 502
    conn.close()
    return jsonify(result)


def run_sync(conn, acct, client, cal_id, register_channels=True, address=None):
    """One full exchange with Google: pull the chosen calendars, pull Vesta's own,
    then push. Shared by the Sync button and by a push notification, so the two can
    never drift into doing different things.
    """
    try:
        # Every calendar the student picked, mirrored into Vesta. This is the half that
        # makes a connected account actually show something: without it Vesta only ever
        # looked at the calendar it made for itself, which is empty until Vesta fills it.
        absorbed = {"added": 0, "updated": 0, "removed": 0, "unreadable": 0}
        sid = active_semester_id(conn)
        for feed in gsync.feeds(conn, acct["id"], enabled_only=True):
            if feed["calendar_id"] == cal_id:
                continue                  # Vesta's own calendar is handled below
            try:
                try:
                    f_raw, f_token = _pull(client, feed["calendar_id"], feed["sync_token"])
                except gcal.GoogleError as e:
                    if not e.resync:
                        raise
                    f_raw, f_token = _pull(client, feed["calendar_id"], None)
                got = gsync.absorb(conn, acct["id"], feed, f_raw, sid)
                for k in absorbed:
                    absorbed[k] += got.get(k, 0)
                conn.execute(
                    "UPDATE calendar_feeds SET sync_token=?, last_sync=?, last_error=NULL"
                    " WHERE id=?", (f_token, now(), feed["id"]))
            except gcal.GoogleError as e:
                # One unreadable calendar should not stop the others, or a single
                # revoked share would make the whole sync look broken.
                conn.execute("UPDATE calendar_feeds SET last_error=? WHERE id=?",
                             (e.message, feed["id"]))
            conn.commit()

        # Pull Vesta's own calendar second: a two-sided change has to be known before
        # anything is pushed, or Vesta would overwrite a change it had not noticed yet.
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
        # Registering happens after a good sync, so a channel is only ever asked for
        # on an account that is actually working.
        channels = ensure_channels(conn, client, acct, cal_id, address) if register_channels else None
    except gcal.GoogleError:
        raise
    counts = gsync.summarise(pushed)
    counts.update({"fromGoogle": gsync.summarise(pulled), "absorbed": absorbed})
    return {"ok": True, "counts": counts, "needsReview": len(review),
            "absorbed": absorbed, "push": channels}

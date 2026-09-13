"""Deciding what to sync, without talking to anyone.

The network lives in `gcal.Client` and in the routes. This module answers the questions
that have judgement in them: which work belongs in Google, what has changed since the
two sides last agreed, and what to do when both of them moved. Everything here takes a
database connection and plain dicts, so it can be tested against the real schema with no
credential and no network, which is the half that is worth testing.

The rule, chosen deliberately: **Vesta wins, and the difference is flagged.** A change
made only in Vesta is pushed. A change made only in Google is recorded as
`remote_ahead` and surfaced, not applied silently. A change made in both is a
`conflict`: Vesta's version stands and the row says so. Nothing is ever overwritten in
either direction without it being visible somewhere.
"""
import json
import uuid
from datetime import datetime

import gcal


def now():
    return datetime.utcnow().isoformat()


def account(conn, account_id=None):
    """The connected Google account, or None. There is only ever one locally."""
    if account_id:
        return conn.execute("SELECT * FROM calendar_accounts WHERE id=?", (account_id,)).fetchone()
    return conn.execute(
        "SELECT * FROM calendar_accounts WHERE provider='google' ORDER BY created_at LIMIT 1"
    ).fetchone()


def links_by(conn, account_id, kind, key="local_id"):
    rows = conn.execute("SELECT * FROM sync_links WHERE account_id=? AND local_kind=?",
                        (account_id, kind)).fetchall()
    return {r[key]: r for r in rows if r[key]}


def item_dict(row):
    """A database row as the plain dict `gcal` expects, tolerating older schemas."""
    keys = row.keys()
    return {
        "id": row["id"],
        "title": row["title"],
        "dueDate": row["due_date"],
        "dueTime": row["due_time"],
        "type": row["type"],
        "status": row["status"],
        "notes": row["notes"] or "",
        "location": (row["location"] if "location" in keys else "") or "",
    }


# ---------------------------------------------------------------------------
# Push: what of Vesta's belongs in Google, and what has moved since last time
# ---------------------------------------------------------------------------
def plan_push(conn, account_id, tz=gcal.TZ):
    """Actions to take against Google, decided entirely from local state.

    Only the local side is compared here, because the remote hash is not knowable
    without a pull. A real two-sided conflict is detected during the pull, which marks
    the link; this function then refuses to clobber it and reports it instead.
    """
    classes = {c["id"]: dict(c) for c in conn.execute("SELECT * FROM classes")}
    rows = conn.execute(
        "SELECT * FROM items WHERE due_date IS NOT NULL AND due_date != ''"
    ).fetchall()
    links = links_by(conn, account_id, "item")

    plan = []
    for r in rows:
        item = item_dict(r)
        if not gcal.should_push(item):
            continue
        body = gcal.item_body(item, classes.get(r["class_id"]), tz)
        local = gcal.local_hash(body)
        link = links.pop(r["id"], None)

        if link is None:
            plan.append({"action": "create", "kind": "item", "localId": r["id"],
                         "title": item["title"], "body": body, "localHash": local})
        elif link["state"] == "conflict":
            plan.append({"action": "conflict", "kind": "item", "localId": r["id"],
                         "title": item["title"], "externalId": link["external_id"],
                         "body": body, "localHash": local,
                         "reason": "changed in both Vesta and Google since the last sync"})
        elif local != link["local_hash"]:
            plan.append({"action": "update", "kind": "item", "localId": r["id"],
                         "title": item["title"], "externalId": link["external_id"],
                         "body": body, "localHash": local})
        else:
            plan.append({"action": "skip", "kind": "item", "localId": r["id"],
                         "title": item["title"], "externalId": link["external_id"]})

    # anything still in `links` no longer qualifies: deleted, or its due date was cleared
    for local_id, link in links.items():
        plan.append({"action": "remove", "kind": "item", "localId": local_id,
                     "externalId": link["external_id"],
                     "reason": "no longer has a due date in Vesta"})
    return plan


# ---------------------------------------------------------------------------
# Pull: what Google has that Vesta does not, and what disagrees
# ---------------------------------------------------------------------------
def plan_pull(conn, account_id, events, tz=gcal.TZ):
    """Classify what came back from Google against what Vesta last agreed to.

    Events Vesta created are only interesting when they have drifted. Events Vesta has
    never seen are someone else's: they are offered for review rather than written,
    because a calendar full of dentist appointments is not coursework.
    """
    links = links_by(conn, account_id, "item", key="external_id")
    out = []
    for raw in events or []:
        ev = gcal.from_google(raw, tz)
        if ev is None:
            out.append({"action": "unreadable", "externalId": raw.get("id"),
                        "reason": "no start date Vesta could read"})
            continue
        link = links.get(ev["externalId"])
        if ev["cancelled"]:
            out.append({"action": "deleted_remotely" if link else "ignore",
                        "externalId": ev["externalId"], "title": ev["title"],
                        "localId": link["local_id"] if link else None})
            continue
        if link is None:
            out.append({"action": "review", "externalId": ev["externalId"],
                        "title": ev["title"], "event": ev,
                        "reason": "created in Google, not by Vesta"})
            continue
        if ev["hash"] == link["remote_hash"]:
            out.append({"action": "clean", "externalId": ev["externalId"], "title": ev["title"],
                        "localId": link["local_id"]})
            continue
        # it moved on Google's side. Whether that is a conflict depends on whether Vesta
        # moved too, which is exactly what the stored local hash answers.
        local_now = current_local_hash(conn, link["local_id"], tz)
        verdict = "conflict" if (local_now and local_now != link["local_hash"]) else "remote_ahead"
        out.append({"action": verdict, "externalId": ev["externalId"], "title": ev["title"],
                    "localId": link["local_id"], "event": ev,
                    "reason": ("changed in both places" if verdict == "conflict"
                               else "changed in Google")})
    return out


def current_local_hash(conn, local_id, tz=gcal.TZ):
    row = conn.execute("SELECT * FROM items WHERE id=?", (local_id,)).fetchone()
    if row is None:
        return None
    cls = conn.execute("SELECT * FROM classes WHERE id=?", (row["class_id"],)).fetchone()
    return gcal.local_hash(gcal.item_body(item_dict(row), dict(cls) if cls else None, tz))


# ---------------------------------------------------------------------------
# Recording what happened
# ---------------------------------------------------------------------------
def record(conn, account_id, kind, local_id, external_id, local_hash, remote_hash,
           etag="", state="clean", remote_updated=""):
    """Upsert the link, which is what makes the next sync able to tell what moved."""
    existing = conn.execute(
        "SELECT id FROM sync_links WHERE account_id=? AND local_kind=? AND local_id=?",
        (account_id, kind, local_id)).fetchone()
    if existing:
        conn.execute(
            "UPDATE sync_links SET external_id=?, etag=?, local_hash=?, remote_hash=?,"
            " remote_updated=?, last_synced=?, state=? WHERE id=?",
            (external_id, etag, local_hash, remote_hash, remote_updated, now(), state,
             existing["id"]))
    else:
        conn.execute(
            "INSERT INTO sync_links (id, account_id, local_kind, local_id, external_id, etag,"
            " local_hash, remote_hash, remote_updated, last_synced, state)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), account_id, kind, local_id, external_id, etag, local_hash,
             remote_hash, remote_updated, now(), state))


def forget(conn, account_id, kind, local_id):
    conn.execute("DELETE FROM sync_links WHERE account_id=? AND local_kind=? AND local_id=?",
                 (account_id, kind, local_id))


def mark(conn, account_id, external_id, state):
    conn.execute("UPDATE sync_links SET state=? WHERE account_id=? AND external_id=?",
                 (state, account_id, external_id))


def summarise(plan):
    """Counts by action, for telling the student what a sync actually did."""
    out = {}
    for p in plan:
        out[p["action"]] = out.get(p["action"], 0) + 1
    return out

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
        if link is None and not ev["cancelled"] and made_by_vesta(ev):
            # Vesta's own event, whose link was lost. Offering it for review as though
            # somebody else had made it is what put "5 event(s) from Google Calendar" on
            # the Classes page after the Invalid start time failures: each failed sync
            # had created events and then thrown away the record of creating them.
            out.append({"action": "orphan", "externalId": ev["externalId"],
                        "title": ev["title"], "event": ev})
            continue
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
def made_by_vesta(ev):
    """Whether an event (as `gcal.from_google` returns it) is one Vesta wrote."""
    return (ev.get("notes") or "").rstrip().endswith(gcal.VESTA_MARK)


def _plain_summary(s):
    s = (s or "").strip()
    return s[len(gcal.DONE_MARK):].strip() if s.startswith(gcal.DONE_MARK) else s


def adopt(conn, account_id, ev, tz=gcal.TZ):
    """Reconnect one of Vesta's lost events to the assignment it was made for.

    Only an assignment with no event of its own is a candidate, matched on the date and
    the summary Vesta would write for it now (ignoring the done tick, which may have
    changed since). Returns the assignment's id, or None when every candidate already
    has its event, in which case this one is a duplicate. The link is stored with an
    empty local hash, so the push that follows brings the event up to date.
    """
    linked = {r["local_id"] for r in conn.execute(
        "SELECT local_id FROM sync_links WHERE account_id=? AND local_kind='item'", (account_id,))}
    classes = {c["id"]: dict(c) for c in conn.execute("SELECT * FROM classes")}
    for r in conn.execute("SELECT * FROM items WHERE due_date=?", (ev["date"],)).fetchall():
        if r["id"] in linked:
            continue
        body = gcal.item_body(item_dict(r), classes.get(r["class_id"]), tz)
        if _plain_summary(body.get("summary")) == _plain_summary(ev["title"]):
            record(conn, account_id, "item", r["id"], ev["externalId"], "", ev["hash"],
                   ev.get("etag") or "")
            return r["id"]
    return None


def prune_reviews(conn):
    """Take Vesta's own events out of any Google batch waiting for review.

    A batch made before Vesta could recognise its own events may hold nothing else, and
    one that ends up empty is removed, so its banner goes with it.
    """
    import json
    for row in conn.execute("SELECT id, draft FROM calendar_imports"
                            " WHERE source='google' AND status='review'").fetchall():
        try:
            draft = json.loads(row["draft"] or "{}")
        except ValueError:
            continue
        events = draft.get("events") or []
        keep = [e for e in events if not made_by_vesta(e)]
        if len(keep) == len(events):
            continue
        if not keep:
            conn.execute("DELETE FROM calendar_imports WHERE id=?", (row["id"],))
        else:
            draft["events"] = keep
            conn.execute("UPDATE calendar_imports SET draft=?, label=? WHERE id=?",
                         (json.dumps(draft, default=str),
                          "%d event(s) from Google Calendar" % len(keep), row["id"]))


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


# ---------------------------------------------------------------------------
# Feeds: which of the student's calendars Vesta reads
# ---------------------------------------------------------------------------
def feeds(conn, account_id, enabled_only=False):
    sql = "SELECT * FROM calendar_feeds WHERE account_id=?"
    if enabled_only:
        sql += " AND enabled=1"
    return conn.execute(sql + " ORDER BY is_vesta DESC, name", (account_id,)).fetchall()


def merge_feeds(conn, account_id, listing):
    """Reconcile what Google says the account has against what Vesta already knew.

    A calendar Vesta has not seen before arrives switched **off**. Reading somebody's
    entire Google account the moment they connect it is not a decision Vesta gets to
    make on their behalf: the picker is the consent, so the default has to be no.

    The exception is Vesta's own calendar, which it created and already writes to.
    """
    known = {r["calendar_id"]: r for r in feeds(conn, account_id)}
    seen = set()
    for c in listing:
        cid = c.get("calendarId")
        if not cid:
            continue
        seen.add(cid)
        row = known.get(cid)
        if row is None:
            conn.execute(
                "INSERT INTO calendar_feeds (id, account_id, calendar_id, name, colour,"
                " writable, is_vesta, enabled, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), account_id, cid, c.get("name") or cid,
                 c.get("colour") or "", 1 if c.get("writable") else 0,
                 1 if c.get("isVesta") else 0, 1 if c.get("isVesta") else 0, now()))
        else:
            # The name, colour and access role are Google's to change, not Vesta's to
            # remember wrongly. `enabled` is the student's and is left alone.
            conn.execute(
                "UPDATE calendar_feeds SET name=?, colour=?, writable=?, is_vesta=? WHERE id=?",
                (c.get("name") or cid, c.get("colour") or "",
                 1 if c.get("writable") else 0, 1 if c.get("isVesta") else 0, row["id"]))
    # A calendar that has disappeared from Google is dropped, along with its sync token,
    # so re-subscribing later starts clean rather than resuming from a stale cursor.
    for cid, row in known.items():
        if cid not in seen:
            conn.execute("DELETE FROM calendar_feeds WHERE id=?", (row["id"],))
    conn.commit()
    return feeds(conn, account_id)


def set_enabled(conn, account_id, calendar_ids):
    """Turn the chosen calendars on and every other one off.

    Switching a calendar off clears its sync token: if it is ever switched back on,
    Vesta should re-read it in full rather than resume from a cursor that skipped
    everything that happened while it was ignored.
    """
    chosen = set(calendar_ids or [])
    for row in feeds(conn, account_id):
        want = 1 if row["calendar_id"] in chosen else 0
        if want == (row["enabled"] or 0):
            continue
        if want:
            conn.execute("UPDATE calendar_feeds SET enabled=1 WHERE id=?", (row["id"],))
        else:
            # Withdraw what it mirrored in. Leaving the events behind would strand them:
            # they are read-only, so they could never be corrected, and they would never
            # update again, so they would quietly drift out of date forever.
            conn.execute("DELETE FROM events WHERE feed_id=?", (row["id"],))
            conn.execute(
                "UPDATE calendar_feeds SET enabled=0, sync_token=NULL WHERE id=?", (row["id"],))
    conn.commit()
    return feeds(conn, account_id)


# ---------------------------------------------------------------------------
# Absorbing a calendar the student asked for
# ---------------------------------------------------------------------------
def absorb(conn, account_id, feed, raw_events, semester_id, tz=gcal.TZ):
    """Mirror one chosen calendar's events into Vesta's own `events` table.

    This is deliberately not `plan_pull`. That function treats an event Vesta did not
    create as somebody else's business and offers it for review, which is right for a
    calendar Vesta stumbled upon and wrong for one the student explicitly picked. Having
    chosen it, they mean "show me this", so it lands.

    Landed events are marked `read_only`: they belong to Google, and the way to change
    one is to change it there. Deletions on Google delete here, since a mirror that
    keeps what the original dropped stops being a mirror.
    """
    stats = {"added": 0, "updated": 0, "removed": 0, "unreadable": 0}
    for raw in raw_events or []:
        ext = raw.get("id")
        if not ext:
            continue
        existing = conn.execute(
            "SELECT id FROM events WHERE account_id=? AND external_id=?",
            (account_id, ext)).fetchone()
        if (raw.get("status") == "cancelled"):
            if existing:
                conn.execute("DELETE FROM events WHERE id=?", (existing["id"],))
                stats["removed"] += 1
            continue
        ev = gcal.from_google(raw, tz)
        if ev is None:
            stats["unreadable"] += 1
            continue
        if existing:
            conn.execute(
                "UPDATE events SET title=?, date=?, start=?, \"end\"=?, all_day=?,"
                " location=?, notes=?, updated_at=? WHERE id=?",
                (ev["title"], ev["date"], ev["start"] or "", ev["end"] or "",
                 1 if ev["allDay"] else 0, ev["location"], ev["notes"], now(),
                 existing["id"]))
            stats["updated"] += 1
        else:
            conn.execute(
                "INSERT INTO events (id, semester_id, class_id, title, kind, date, start,"
                " \"end\", all_day, location, notes, created_at, source, account_id,"
                " external_id, feed_id, read_only, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), semester_id, None, ev["title"], "external",
                 ev["date"], ev["start"] or "", ev["end"] or "",
                 1 if ev["allDay"] else 0, ev["location"], ev["notes"], now(),
                 "google", account_id, ext, feed["id"], 1, now()))
            stats["added"] += 1
    return stats

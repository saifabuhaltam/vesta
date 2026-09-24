"""Vesta knows its own Google events even after losing the link to them.

The "Invalid start time" failures left events in Saif's Vesta calendar that Vesta had
created and then forgotten: each failed sync rolled back the record of creating them.
The next good sync offered them on the Classes page as "5 event(s) from Google
Calendar", as though somebody else had made them, and opening that crashed, because
it went to SFU's timetable screen. These tests replay that and its cures: lost events
are reconnected or, if they are duplicates, removed; a pending batch of them is
cleaned away; a sync that stops part way keeps what it had already done; and a batch
of events that really are someone else's has a review that works.
"""
import json
import uuid

import pytest

import calendar_api
import gcal
import gsync
from test_calendar_patch import MergingGoogle, add_item, sync, conn          # noqa: F401
from test_calendar_push import account, client                              # noqa: F401


class ListingGoogle(MergingGoogle):
    """Also answers a pull with the events it holds, as Google does."""

    def list_events(self, calendar_id, sync_token=None, page_token=None, time_min=None):
        return {"items": [dict(e, etag="1") for e in self.events.values()], "nextSyncToken": "t"}


def links(conn):
    return conn.execute("SELECT local_id, external_id FROM sync_links").fetchall()


@pytest.fixture(autouse=True)
def no_batches(conn):
    conn.execute("DELETE FROM calendar_imports")
    conn.commit()
    yield
    conn.execute("DELETE FROM calendar_imports")
    conn.commit()


# ---------------------------------------------------------------------------

def test_a_lost_event_of_vesta_s_is_not_offered_as_someone_else_s(conn, account):
    google = ListingGoogle()
    iid = add_item(conn, "CAL Reading quiz (Week 1)", "2026-09-13", "23:59")
    sync(conn, account, google)
    conn.execute("DELETE FROM sync_links")          # what a failed sync used to leave
    conn.commit()
    result = sync(conn, account, google)
    assert result["needsReview"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM calendar_imports").fetchone()["n"] == 0
    assert len(google.events) == 1                  # reconnected, not made again
    assert [r["local_id"] for r in links(conn)] == [iid]
    assert result["counts"]["tidied"] == {"reconnected": 1, "duplicatesRemoved": 0}


def test_a_duplicate_vesta_made_is_removed_from_google(conn, account):
    """The failed runs also made second copies. The one Vesta tracks stays."""
    google = ListingGoogle()
    add_item(conn, "CAL Reading quiz (Week 1)", "2026-09-13", "23:59")
    sync(conn, account, google)
    kept = next(iter(google.events))
    stray = dict(google.events[kept])
    stray["id"] = "ev-stray"
    google.events["ev-stray"] = stray
    result = sync(conn, account, google)
    assert list(google.events) == [kept]
    assert result["counts"]["tidied"]["duplicatesRemoved"] == 1


def test_an_event_he_made_himself_is_still_offered(conn, account):
    google = ListingGoogle()
    google.events["ev-mine"] = {"id": "ev-mine", "summary": "Study group",
                                "start": {"dateTime": "2026-09-25T18:00:00-07:00"},
                                "end": {"dateTime": "2026-09-25T19:00:00-07:00"},
                                "description": "Bring notes"}
    result = sync(conn, account, google)
    assert result["needsReview"] == 1
    assert "ev-mine" in google.events


def test_a_waiting_batch_of_vesta_s_own_events_is_cleared(conn, account):
    """The batch already on his Classes page, made before Vesta knew its own."""
    ours = {"externalId": "a", "title": "PHIL 110: Midterm", "date": "2026-10-19",
            "notes": "Exam\n\nAdded by Vesta"}
    theirs = {"externalId": "b", "title": "Study group", "date": "2026-09-25", "notes": ""}
    for events in ([ours], [ours, theirs]):
        bid = str(uuid.uuid4())
        conn.execute("INSERT INTO calendar_imports (id, source, label, draft, status, created_at)"
                     " VALUES (?,?,?,?,?,?)", (bid, "google", "%d event(s) from Google Calendar" % len(events),
                                               json.dumps({"source": "google", "events": events}), "review",
                                               "2026-09-23"))
    conn.commit()
    gsync.prune_reviews(conn)
    conn.commit()
    rows = conn.execute("SELECT label, draft FROM calendar_imports").fetchall()
    assert len(rows) == 1
    assert rows[0]["label"] == "1 event(s) from Google Calendar"
    assert [e["externalId"] for e in json.loads(rows[0]["draft"])["events"]] == ["b"]


def test_a_sync_that_stops_part_way_keeps_what_it_did(conn, account):
    """The root of the leftovers: a failed sync rolled back the links it had made."""
    class DiesOnSecond(MergingGoogle):
        def insert(self, calendar_id, body):
            if len(self.events) >= 1:
                raise gcal.GoogleError("Google Calendar said no: Invalid Credentials", 401)
            return super().insert(calendar_id, body)

    first = add_item(conn, "CAL A", "2026-09-10")
    add_item(conn, "CAL B", "2026-09-11")
    with pytest.raises(gcal.GoogleError):
        sync(conn, account, DiesOnSecond())
    from db import get_db
    fresh = get_db()
    kept = [r["local_id"] for r in fresh.execute("SELECT local_id FROM sync_links")]
    fresh.close()
    assert kept == [first]


# ---------------------------------------------------------------------------
# the review for events that really are someone else's
# ---------------------------------------------------------------------------

def test_choosing_events_from_a_google_batch_adds_them_and_finishes_it(conn):
    bid = str(uuid.uuid4())
    events = [{"externalId": "b1", "title": "Study group", "date": "2026-09-25",
               "start": "18:00", "end": "19:00", "allDay": False, "notes": ""},
              {"externalId": "b2", "title": "Dentist", "date": "2026-09-26", "allDay": True}]
    conn.execute("INSERT INTO calendar_imports (id, source, label, draft, status, created_at)"
                 " VALUES (?,?,?,?,?,?)", (bid, "google", "2 event(s) from Google Calendar",
                                           json.dumps({"source": "google", "events": events}),
                                           "review", "2026-09-23"))
    conn.commit()
    got = conn.execute("SELECT * FROM calendar_imports WHERE id=?", (bid,)).fetchone()
    with _app_ctx():
        r = calendar_api._apply_google_batch(conn_for_route(), got, {"externalIds": ["b1"]})
    body = r.get_json()
    assert body == {"ok": True, "added": 1}
    ev = conn.execute("SELECT title, date, start, \"end\" FROM events WHERE external_id='b1'").fetchone()
    assert (ev["title"], ev["date"], ev["start"], ev["end"]) == ("Study group", "2026-09-25", "18:00", "19:00")
    assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE external_id='b2'").fetchone()["n"] == 0
    assert conn.execute("SELECT status FROM calendar_imports WHERE id=?", (bid,)).fetchone()["status"] == "imported"
    conn.execute("DELETE FROM events WHERE external_id IN ('b1','b2')")
    conn.commit()


def _app_ctx():
    import app as vesta_app
    return vesta_app.app.test_request_context()


def conn_for_route():
    from db import get_db
    return get_db()

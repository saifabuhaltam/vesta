"""Google Calendar: an assignment that gains or loses a due time still syncs, and one
event Google refuses does not stop the rest.

Saif hit "Google Calendar said no: Invalid start time." after the Canvas review filled
due times into assignments his syllabus had given only dates. Vesta updates an event
with PATCH, which merges into what Google holds, so an all-day event's `date` survived
beside the new `dateTime`, and Google rejects an event with both. The fake below
merges exactly the way Google does and refuses exactly what Google refuses, so these
tests fail without the fix.
"""
import copy
import uuid

import pytest

import calendar_api
import gcal
from test_calendar_push import account, client     # noqa: F401


class MergingGoogle:
    """Holds events, merges PATCH bodies the way Google does, and refuses what it
    refuses: a start or end with both a date and a dateTime."""

    def __init__(self, refuse_titles=()):
        self.events = {}
        self.refuse = set(refuse_titles)

    def _check(self, ev):
        if any(t in (ev.get("summary") or "") for t in self.refuse):
            raise gcal.GoogleError("Google Calendar said no: Invalid start time.", 400)
        for key in ("start", "end"):
            part = ev.get(key) or {}
            if part.get("date") and part.get("dateTime"):
                raise gcal.GoogleError("Google Calendar said no: Invalid start time.", 400)

    def list_events(self, calendar_id, sync_token=None, page_token=None, time_min=None):
        return {"items": [], "nextSyncToken": "t"}

    def insert(self, calendar_id, body):
        ev = copy.deepcopy(body)
        self._check(ev)
        ev["id"] = "ev-" + str(len(self.events) + 1)
        self.events[ev["id"]] = ev
        return dict(ev, etag="1")

    def patch(self, calendar_id, event_id, body):
        ev = copy.deepcopy(self.events[event_id])
        for key, value in body.items():
            if isinstance(value, dict) and isinstance(ev.get(key), dict):
                merged = dict(ev[key])
                for k, v in value.items():
                    if v is None:
                        merged.pop(k, None)      # null clears the field
                    else:
                        merged[k] = v
                ev[key] = merged
            else:
                ev[key] = value
        self._check(ev)
        self.events[event_id] = ev
        return dict(ev, etag="2")

    def delete(self, calendar_id, event_id):
        self.events.pop(event_id, None)


@pytest.fixture
def conn():
    from db import get_db
    c = get_db()
    c.execute("DELETE FROM sync_links")
    c.execute("DELETE FROM items WHERE title LIKE 'CAL %'")
    c.commit()
    yield c
    c.execute("DELETE FROM items WHERE title LIKE 'CAL %'")
    c.execute("DELETE FROM sync_links")
    c.commit()
    c.close()


def add_item(conn, title, date, time=None):
    from db import active_semester_id
    iid = str(uuid.uuid4())
    conn.execute("INSERT INTO items (id, semester_id, title, type, due_date, due_time, status,"
                 " created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (iid, active_semester_id(conn), title, "quiz", date, time, "todo", "2026-09-01"))
    conn.commit()
    return iid


def sync(conn, account, google):
    acct = conn.execute("SELECT * FROM calendar_accounts WHERE id=?", (account["id"],)).fetchone()
    return calendar_api.run_sync(conn, acct, google, "vesta-cal", register_channels=False)


def only_event(google):
    assert len(google.events) == 1
    return next(iter(google.events.values()))


# ---------------------------------------------------------------------------

def test_a_patch_clears_the_start_form_it_is_not_using():
    timed = gcal.patch_body({"start": {"dateTime": "2026-09-13T23:59:00", "timeZone": "X"},
                             "end": {"dateTime": "2026-09-14T00:29:00", "timeZone": "X"}})
    assert timed["start"]["date"] is None and timed["end"]["date"] is None
    allday = gcal.patch_body({"start": {"date": "2026-09-13"}, "end": {"date": "2026-09-14"}})
    assert allday["start"]["dateTime"] is None and allday["start"]["timeZone"] is None


def test_an_assignment_that_gains_a_due_time_still_syncs(conn, account):
    """His case: dated by the syllabus, timed by Canvas."""
    google = MergingGoogle()
    iid = add_item(conn, "CAL Reading quiz (Week 1)", "2026-09-13")
    sync(conn, account, google)
    assert "date" in only_event(google)["start"]

    conn.execute("UPDATE items SET due_time='23:59' WHERE id=?", (iid,))
    conn.commit()
    result = sync(conn, account, google)
    ev = only_event(google)
    assert ev["start"] == {"dateTime": "2026-09-13T23:59:00", "timeZone": gcal.TZ}
    assert result["failed"] == []
    assert conn.execute("SELECT last_error FROM calendar_accounts WHERE id=?",
                        (account["id"],)).fetchone()["last_error"] is None


def test_an_assignment_that_loses_its_due_time_still_syncs(conn, account):
    google = MergingGoogle()
    iid = add_item(conn, "CAL Midterm", "2026-10-19", "12:20")
    sync(conn, account, google)
    conn.execute("UPDATE items SET due_time=NULL WHERE id=?", (iid,))
    conn.commit()
    sync(conn, account, google)
    assert only_event(google)["start"] == {"date": "2026-10-19"}


def test_without_the_fix_google_refuses_it(conn, account, monkeypatch):
    """Proof the fake is faithful: the old PATCH body is refused, as it was for him."""
    monkeypatch.setattr(gcal, "patch_body", lambda body: body)
    google = MergingGoogle()
    iid = add_item(conn, "CAL Reading quiz (Week 1)", "2026-09-13")
    sync(conn, account, google)
    conn.execute("UPDATE items SET due_time='23:59' WHERE id=?", (iid,))
    conn.commit()
    result = sync(conn, account, google)
    assert result["failed"][0]["error"].endswith("Invalid start time.")


def test_one_refused_event_does_not_stop_the_rest_and_is_named(conn, account):
    google = MergingGoogle(refuse_titles=["CAL Broken"])
    add_item(conn, "CAL First", "2026-09-10")
    add_item(conn, "CAL Broken", "2026-09-11")
    add_item(conn, "CAL Last", "2026-09-12")
    result = sync(conn, account, google)
    titles = sorted(e["summary"] for e in google.events.values())
    assert titles == ["CAL First", "CAL Last"]
    assert [f["title"] for f in result["failed"]] == ["CAL Broken"]
    err = conn.execute("SELECT last_error FROM calendar_accounts WHERE id=?",
                       (account["id"],)).fetchone()["last_error"]
    assert err == ("1 assignment could not be sent to Google: CAL Broken (Invalid start time.). "
                   "Everything else synced.")


def test_a_dead_token_still_stops_the_sync(conn, account):
    class Revoked(MergingGoogle):
        def insert(self, calendar_id, body):
            raise gcal.GoogleError("Google Calendar said no: Invalid Credentials", 401)
    add_item(conn, "CAL First", "2026-09-10")
    with pytest.raises(gcal.GoogleError):
        sync(conn, account, Revoked())

"""Google push notifications: channels, the webhook, and renewal.

Google is stubbed throughout. What is worth proving here is the handling, because
every one of these was written down as a trap before it could bite:

  - the first notification after registering is a handshake and means nothing;
  - the endpoint is public, so an unknown channel id must do nothing at all;
  - stopping a channel needs the channel id *and* the resource id, so both are stored;
  - a channel near expiry is renewed, and one with a week left is left alone, because
    Google never renews a channel and a missed renewal is silent.
"""
import uuid
from datetime import datetime, timedelta

import pytest

import app as vesta_app
import calendar_api
import gcal


class FakeClient:
    """Enough of gcal.Client to register, renew and stop channels."""

    def __init__(self, expiry_days=7):
        self.watched = []
        self.stopped = []
        self.expiry_days = expiry_days

    def watch(self, calendar_id, channel_id, address, token=None, ttl_seconds=None):
        self.watched.append({"calendarId": calendar_id, "channelId": channel_id,
                             "address": address, "ttl": ttl_seconds})
        when = datetime.utcnow() + timedelta(days=self.expiry_days)
        return {"resourceId": "res-" + channel_id[:6],
                "expiration": str(int(when.timestamp() * 1000))}

    def stop_channel(self, channel_id, resource_id):
        self.stopped.append((channel_id, resource_id))
        return {}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("INVITE_EMAILS", raising=False)
    vesta_app.app.config["TESTING"] = True
    with vesta_app.app.test_client() as c:
        yield c


@pytest.fixture
def account():
    """A connected Google account with one chosen calendar."""
    from db import get_db
    conn = get_db()
    conn.execute("DELETE FROM calendar_channels")
    conn.execute("DELETE FROM calendar_feeds")
    conn.execute("DELETE FROM calendar_accounts")
    aid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO calendar_accounts (id, provider, name, access_token, refresh_token,"
        " token_expires, calendar_id, enabled, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (aid, "google", "saif@example.com", "tok", "refresh",
         (datetime.utcnow() + timedelta(hours=1)).isoformat(), "vesta-cal", 1,
         datetime.utcnow().isoformat()))
    conn.execute(
        "INSERT INTO calendar_feeds (id, account_id, calendar_id, name, enabled, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), aid, "school-cal", "School", 1, datetime.utcnow().isoformat()))
    conn.commit()
    row = conn.execute("SELECT * FROM calendar_accounts WHERE id=?", (aid,)).fetchone()
    conn.close()
    return row


def channels():
    from db import get_db
    conn = get_db()
    rows = conn.execute("SELECT * FROM calendar_channels ORDER BY calendar_id").fetchall()
    conn.close()
    return rows


def test_a_channel_is_registered_for_every_watched_calendar(account):
    from db import get_db
    conn = get_db()
    fake = FakeClient()
    out = calendar_api.ensure_channels(conn, fake, account, "vesta-cal",
                                       address="https://vesta.study/api/calendar/google/webhook")
    conn.close()
    assert out["registered"] == 2                     # the chosen calendar, plus Vesta's own
    assert {w["calendarId"] for w in fake.watched} == {"school-cal", "vesta-cal"}
    assert all(w["address"].startswith("https://") for w in fake.watched)
    rows = channels()
    assert len(rows) == 2
    # both ids stored: stopping a channel needs the pair
    assert all(r["channel_id"] and r["resource_id"] for r in rows)
    assert all(r["expiration"] for r in rows)


def test_a_healthy_channel_is_left_alone_and_a_dying_one_is_renewed(account):
    from db import get_db
    conn = get_db()
    calendar_api.ensure_channels(conn, FakeClient(), account, "vesta-cal",
                                 address="https://vesta.study/hook")
    before = {r["calendar_id"]: r["channel_id"] for r in channels()}

    again = FakeClient()
    out = calendar_api.ensure_channels(conn, again, account, "vesta-cal",
                                       address="https://vesta.study/hook")
    assert out == {"registered": 0, "renewed": 0, "failed": 0}
    assert again.watched == []                        # nothing re-registered

    # now age one of them to within the renewal window
    conn.execute("UPDATE calendar_channels SET expiration=? WHERE calendar_id='school-cal'",
                 ((datetime.utcnow() + timedelta(hours=2)).isoformat(),))
    conn.commit()
    third = FakeClient()
    out = calendar_api.ensure_channels(conn, third, account, "vesta-cal",
                                       address="https://vesta.study/hook")
    conn.close()
    assert out["renewed"] == 1 and out["registered"] == 0
    assert [w["calendarId"] for w in third.watched] == ["school-cal"]
    # the old channel is closed, with both ids, so it stops calling
    assert third.stopped and third.stopped[0][0] == before["school-cal"]
    assert channels()[0]["channel_id"] != before["school-cal"]


def test_registering_survives_google_refusing(account):
    from db import get_db

    class Refuses(FakeClient):
        def watch(self, *a, **k):
            raise gcal.GoogleError("Calendar usage limits exceeded", 403)

    conn = get_db()
    out = calendar_api.ensure_channels(conn, Refuses(), account, "vesta-cal",
                                       address="https://vesta.study/hook")
    conn.close()
    assert out["failed"] == 2 and out["registered"] == 0
    assert channels() == []            # nothing recorded that does not exist at Google


def test_no_channels_are_registered_without_a_public_address(account):
    from db import get_db
    conn = get_db()
    fake = FakeClient()
    out = calendar_api.ensure_channels(conn, fake, account, "vesta-cal", address="")
    conn.close()
    assert out["skipped"] == "no public address"
    assert fake.watched == []          # localhost cannot receive a notification anyway


def test_the_handshake_ping_does_nothing(client, account, monkeypatch):
    ran = []
    monkeypatch.setattr(calendar_api, "_sync_for_push", lambda uid: ran.append(uid))
    r = client.post("/api/calendar/google/webhook",
                    headers={"X-Goog-Channel-ID": "anything", "X-Goog-Resource-State": "sync"})
    assert r.status_code == 200
    assert ran == []


def test_an_unknown_channel_is_ignored(client, monkeypatch):
    ran = []
    monkeypatch.setattr(calendar_api, "_sync_for_push", lambda uid: ran.append(uid))
    r = client.post("/api/calendar/google/webhook",
                    headers={"X-Goog-Channel-ID": "not-ours", "X-Goog-Resource-State": "exists"})
    assert r.status_code == 200
    assert ran == []


def test_a_real_notification_starts_a_sync(client, account, monkeypatch):
    from db import get_db
    conn = get_db()
    calendar_api.ensure_channels(conn, FakeClient(), account, "vesta-cal",
                                 address="https://vesta.study/hook")
    conn.close()
    known = channels()[0]["channel_id"]

    started = []
    monkeypatch.setattr(calendar_api.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda s: started.append(args)})())
    r = client.post("/api/calendar/google/webhook",
                    headers={"X-Goog-Channel-ID": known, "X-Goog-Resource-State": "exists"})
    assert r.status_code == 200
    assert len(started) == 1           # answered at once, work handed to a thread


def test_health_reports_the_channels_so_a_dead_renewal_is_visible(client, account):
    from db import get_db
    conn = get_db()
    calendar_api.ensure_channels(conn, FakeClient(), account, "vesta-cal",
                                 address="https://vesta.study/hook")
    conn.close()
    push = client.get("/health").get_json()["calendarPush"]
    assert push["channels"] == 2
    assert push["nextExpiry"]

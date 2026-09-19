"""Do two accounts actually stay apart, and does anything outside a request work?

These are the tests that could not be written before: the isolation story is enforced
by Postgres, so SQLite cannot exercise a line of it.
"""
import threading

import psycopg
import pytest

from conftest import ALICE, BOB, DATABASE_URL

import db as vdb


@pytest.fixture(scope="module", autouse=True)
def schema():
    """Build the schema the way a deploy does, then give the two accounts somewhere to hang."""
    vdb.init_db()                       # ensure_pg_schema + run_pg_migrations
    conn = vdb.get_db(user_id=None)
    conn.as_owner()
    for uid, email in ((ALICE, "alice@example.com"), (BOB, "bob@example.com")):
        conn.execute("insert into auth.users (id, email) values (?, ?) "
                     "on conflict (id) do nothing", (uid, email))
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def clean():
    yield
    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    with conn.cursor() as cur:
        for t in ("materials", "classes", "app_settings", "ai_usage", "humanizer_runs"):
            cur.execute(f"alter table {t} no force row level security")
            cur.execute(f"delete from {t}")
            cur.execute(f"alter table {t} force row level security")
    conn.close()


def as_user(uid):
    return vdb.get_db(user_id=uid)


def make_class(uid, cid, name):
    conn = as_user(uid)
    conn.execute("INSERT INTO classes (id, code, name, created_at) VALUES (?,?,?,?)",
                 (cid, "X", name, "2026-01-01"))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# The core promise
# ---------------------------------------------------------------------------
def test_the_test_role_is_not_a_superuser():
    """Guards the suite itself: as a superuser every assertion below passes vacuously."""
    conn = psycopg.connect(DATABASE_URL)
    row = conn.execute("select rolsuper, rolbypassrls from pg_roles where rolname = current_user").fetchone()
    conn.close()
    assert row == (False, False)


def test_a_class_belongs_to_the_account_that_made_it():
    make_class(ALICE, "c-alice", "Alice's class")
    conn = as_user(ALICE)
    rows = conn.execute("SELECT * FROM classes").fetchall()
    conn.close()
    assert [r["name"] for r in rows] == ["Alice's class"]


def test_the_other_account_cannot_see_it():
    make_class(ALICE, "c-alice", "Alice's class")
    conn = as_user(BOB)
    assert conn.execute("SELECT * FROM classes").fetchall() == []
    assert conn.execute("SELECT * FROM classes WHERE id=?", ("c-alice",)).fetchone() is None
    conn.close()


def test_the_other_account_cannot_update_it():
    make_class(ALICE, "c-alice", "Alice's class")
    conn = as_user(BOB)
    assert conn.execute("UPDATE classes SET name=? WHERE id=?", ("stolen", "c-alice")).rowcount == 0
    conn.commit()
    conn.close()
    conn = as_user(ALICE)
    assert conn.execute("SELECT name FROM classes WHERE id=?", ("c-alice",)).fetchone()["name"] == "Alice's class"
    conn.close()


def test_the_other_account_cannot_delete_it():
    make_class(ALICE, "c-alice", "Alice's class")
    conn = as_user(BOB)
    assert conn.execute("DELETE FROM classes WHERE id=?", ("c-alice",)).rowcount == 0
    conn.commit()
    conn.close()
    conn = as_user(ALICE)
    assert conn.execute("SELECT count(*) AS n FROM classes").fetchone()["n"] == 1
    conn.close()


def test_a_row_cannot_be_inserted_in_someone_elses_name():
    """The WITH CHECK half. Without it a client could hand a row to another account."""
    conn = as_user(BOB)
    with pytest.raises(Exception):
        conn.execute("INSERT INTO classes (id, code, name, created_at, user_id) VALUES (?,?,?,?,?)",
                     ("c-forged", "X", "forged", "2026-01-01", ALICE))
        conn.commit()
    conn.close()


def test_a_download_lookup_by_id_is_scoped():
    """How /api/materials/<id>/download stays safe: a guessed id simply finds nothing."""
    conn = as_user(ALICE)
    conn.execute("INSERT INTO materials (id, kind, title, filename, stored_name, created_at)"
                 " VALUES (?,?,?,?,?,?)",
                 ("m-alice", "file", "Notes", "notes.pdf", "stored-alice.pdf", "2026-01-01"))
    conn.commit()
    conn.close()
    conn = as_user(BOB)
    assert conn.execute("SELECT * FROM materials WHERE id=?", ("m-alice",)).fetchone() is None
    conn.close()


def test_settings_are_per_account():
    """The display name lives here, so this is the profile feature's isolation test."""
    for uid, name in ((ALICE, "Alice"), (BOB, "Bob")):
        conn = as_user(uid)
        vdb.set_setting(conn, "display_name", name)
        conn.close()
    for uid, name in ((ALICE, "Alice"), (BOB, "Bob")):
        conn = as_user(uid)
        assert vdb.get_setting(conn, "display_name") == name
        conn.close()


# ---------------------------------------------------------------------------
# Outside a request: the bug that made Office previews never finish
# ---------------------------------------------------------------------------
def test_an_ownerless_connection_can_read_nothing():
    """The root cause, stated directly.

    Forced RLS plus policies granted `to authenticated` mean the owning role is held to
    `user_id = auth.uid()`, and with no claim set `auth.uid()` is null. Every row fails
    the comparison, so a job with no user reads an empty database rather than failing.
    """
    make_class(ALICE, "c-alice", "Alice's class")
    conn = vdb.get_db(user_id=None)
    assert conn.execute("SELECT * FROM classes").fetchall() == []
    conn.close()


def test_an_ownerless_write_silently_touches_nothing():
    """And the reason it went unnoticed: no error, just rowcount 0."""
    make_class(ALICE, "c-alice", "Alice's class")
    conn = vdb.get_db(user_id=None)
    assert conn.execute("UPDATE classes SET name=? WHERE id=?", ("changed", "c-alice")).rowcount == 0
    conn.commit()
    conn.close()


def test_a_background_thread_without_its_user_loses_the_write():
    """Exactly what `make_office_preview` used to do, reproduced."""
    conn = as_user(ALICE)
    conn.execute("INSERT INTO materials (id, kind, filename, stored_name, preview_status, created_at)"
                 " VALUES (?,?,?,?,?,?)",
                 ("m-1", "file", "deck.pptx", "s.pptx", "pending", "2026-01-01"))
    conn.commit()
    conn.close()

    result = {}

    def worker():
        # No Flask request context here, which is the whole point.
        c = vdb.get_db(user_id=None)
        result["rowcount"] = c.execute(
            "UPDATE materials SET preview_status='ready' WHERE id=?", ("m-1",)).rowcount
        c.commit()
        c.close()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result["rowcount"] == 0

    conn = as_user(ALICE)
    assert conn.execute("SELECT preview_status FROM materials WHERE id=?",
                        ("m-1",)).fetchone()["preview_status"] == "pending"
    conn.close()


def test_a_background_thread_carrying_its_user_lands_the_write():
    """And the fix: pass the owner in, because the thread cannot work it out."""
    conn = as_user(ALICE)
    conn.execute("INSERT INTO materials (id, kind, filename, stored_name, preview_status, created_at)"
                 " VALUES (?,?,?,?,?,?)",
                 ("m-2", "file", "deck.pptx", "s.pptx", "pending", "2026-01-01"))
    conn.commit()
    conn.close()

    def worker():
        c = vdb.get_db(user_id=ALICE)
        c.execute("UPDATE materials SET preview_status='ready' WHERE id=?", ("m-2",))
        c.commit()
        c.close()

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    conn = as_user(ALICE)
    assert conn.execute("SELECT preview_status FROM materials WHERE id=?",
                        ("m-2",)).fetchone()["preview_status"] == "ready"
    conn.close()


# ---------------------------------------------------------------------------
# Per-account maintenance and the global spend total
# ---------------------------------------------------------------------------
def test_all_user_ids_sees_every_account():
    assert set(vdb.all_user_ids()) >= {ALICE, BOB}


def test_for_each_account_visits_every_account():
    seen = []
    vdb.for_each_account(seen.append)
    assert set(seen) >= {ALICE, BOB}


def test_for_each_account_reaches_rows_an_ownerless_job_cannot():
    make_class(ALICE, "c-alice", "Alice's class")
    make_class(BOB, "c-bob", "Bob's class")
    found = []

    def visit(uid):
        c = vdb.get_db(user_id=uid)
        found.extend(r["name"] for r in c.execute("SELECT name FROM classes").fetchall())
        c.close()

    vdb.for_each_account(visit)
    assert sorted(found) == ["Alice's class", "Bob's class"]


def test_the_global_ai_total_sums_every_account():
    """The per-account cap cannot see this number, which is why the global cap needs it."""
    import ai
    for uid, tokens in ((ALICE, 1_000_000), (BOB, 3_000_000)):
        conn = vdb.get_db(user_id=uid)
        ai.record_usage(conn, "headstart", "claude-sonnet-5", tokens, 0)
        conn.close()

    cfg = dict(ai.DEFAULT_SETTINGS)
    conn = vdb.get_db(user_id=ALICE)
    mine = ai.spent_today(conn, cfg)["usd"]
    conn.close()

    ai._global_spend["at"] = 0.0          # the cache is not under test here
    everyone = ai.spent_today_everyone(cfg)
    assert mine == pytest.approx(2.0)                  # 1M input tokens at $2/M
    assert everyone == pytest.approx(8.0)              # plus Bob's 3M
    assert everyone > mine


def test_preferences_are_per_account():
    """Settings moved from localStorage onto the account, so they must not bleed."""
    import json
    import prefs

    for uid, theme in ((ALICE, "dark"), (BOB, "light")):
        conn = vdb.get_db(user_id=uid)
        vdb.set_setting(conn, prefs.PREFS_KEY, json.dumps({"theme": theme}))
        conn.close()

    for uid, theme in ((ALICE, "dark"), (BOB, "light")):
        conn = vdb.get_db(user_id=uid)
        assert prefs.read(conn)["theme"] == theme
        conn.close()


def test_humanizer_rewrites_and_voice_samples_are_per_account():
    """Migration 005 built the table with its policies, and the voice sample is per account.

    The sample is someone's own writing, and a rewrite is often a draft they have not
    handed in yet: either one reaching another account would be the worst leak here.
    """
    import humanizer

    for uid, voice, rid in ((ALICE, "Alice writes like this.", "run-a"), (BOB, "Bob writes like that.", "run-b")):
        conn = vdb.get_db(user_id=uid)
        humanizer.save_voice(conn, voice)
        conn.execute("INSERT INTO humanizer_runs (id, title, original, final, tells, created_at)"
                     " VALUES (?,?,?,?,?,?)", (rid, "t", "orig " + uid, "final", "[]", "2026-09-18"))
        conn.commit()
        conn.close()

    for uid, voice, rid in ((ALICE, "Alice writes like this.", "run-a"), (BOB, "Bob writes like that.", "run-b")):
        conn = vdb.get_db(user_id=uid)
        assert humanizer.voice_sample(conn) == voice
        assert [r["id"] for r in conn.execute("SELECT id FROM humanizer_runs").fetchall()] == [rid]
        conn.close()

    conn = vdb.get_db(user_id=BOB)
    assert conn.execute("DELETE FROM humanizer_runs WHERE id='run-a'").rowcount == 0
    conn.commit()
    conn.close()


def test_one_accounts_export_cannot_reach_another():
    make_class(ALICE, "c-alice", "Alice's class")
    make_class(BOB, "c-bob", "Bob's class")
    import prefs

    seen = {}
    for uid in (ALICE, BOB):
        conn = vdb.get_db(user_id=uid)
        rows = conn.execute("SELECT name FROM classes").fetchall()
        seen[uid] = sorted(r["name"] for r in rows)
        conn.close()
    assert seen[ALICE] == ["Alice's class"]
    assert seen[BOB] == ["Bob's class"]
    assert "classes" in prefs.EXPORT_TABLES


# ---------------------------------------------------------------------------
# The global spend ceiling, which is the only thing standing between a shared
# Anthropic key and everyone else's enthusiasm
# ---------------------------------------------------------------------------
def _spend(uid, input_tokens):
    import ai
    conn = vdb.get_db(user_id=uid)
    ai.record_usage(conn, "headstart", "claude-sonnet-5", input_tokens, 0)
    conn.close()


def test_the_global_cap_refuses_before_spending_anything():
    """Neither account is over its own $1, but together they are over the global $1."""
    import ai
    _spend(ALICE, 250_000)        # $0.50 at $2 per million
    _spend(BOB, 300_000)          # $0.60
    ai._global_spend["at"] = 0.0

    conn = vdb.get_db(user_id=ALICE)
    try:
        old = ai.GLOBAL_CAP_USD
        ai.GLOBAL_CAP_USD = 1.00
        with pytest.raises(ai.AiRefused) as caught:
            ai.call_claude(conn, "headstart", "write me an essay", max_tokens=100)
        assert caught.value.payload["reason"] == "global_cap"
        assert caught.value.payload["spentTodayEveryone"] == pytest.approx(1.10)
    finally:
        ai.GLOBAL_CAP_USD = old
        conn.close()


def test_without_a_global_cap_the_per_account_one_is_all_there_is(monkeypatch):
    """The default state, and the reason the variable matters before friends arrive.

    Alice is well under her own dollar, so with no global ceiling nothing stops the
    call on cost grounds and it proceeds to the API. The Anthropic client is stubbed
    to prove that without spending anything: an earlier version of this test had no
    stub, reached the real API, and billed a live key.
    """
    import ai
    import anthropic

    class Boom(Exception):
        pass

    def exploding_client(*a, **kw):
        raise Boom("the cost checks let this through")

    monkeypatch.setattr(anthropic, "Anthropic", exploding_client)

    _spend(ALICE, 250_000)
    _spend(BOB, 300_000)
    ai._global_spend["at"] = 0.0

    conn = vdb.get_db(user_id=ALICE)
    try:
        monkeypatch.setattr(ai, "GLOBAL_CAP_USD", None)
        with pytest.raises(ai.AiRefused) as caught:
            ai.call_claude(conn, "headstart", "write me an essay", max_tokens=100)
        # call_claude wraps anything the client throws in a broad except, so the stub
        # surfaces as a 503 rather than as Boom. What matters is which refusal it is
        # NOT: the spend checks passed and the call went on to the API.
        assert caught.value.payload.get("reason") != "global_cap"
        assert "the cost checks let this through" in caught.value.payload["error"]
    finally:
        conn.close()


def test_the_api_is_never_reached_once_the_global_cap_is_hit(monkeypatch):
    """The cap has to refuse *before* the client is built, or it saves no money."""
    import ai
    import anthropic

    def exploding_client(*a, **kw):
        raise AssertionError("the API was reached despite the global cap")

    monkeypatch.setattr(anthropic, "Anthropic", exploding_client)

    _spend(ALICE, 250_000)
    _spend(BOB, 300_000)
    ai._global_spend["at"] = 0.0

    conn = vdb.get_db(user_id=ALICE)
    try:
        monkeypatch.setattr(ai, "GLOBAL_CAP_USD", 1.00)
        with pytest.raises(ai.AiRefused) as caught:
            ai.call_claude(conn, "headstart", "write me an essay", max_tokens=100)
        assert caught.value.payload["reason"] == "global_cap"
    finally:
        conn.close()

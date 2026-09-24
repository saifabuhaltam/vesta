"""This Week's ticks on Postgres: migration 011 arrives, and two students in the same
course keep their own ticks even though Canvas gives them identical keys."""
import psycopg
import pytest

from conftest import ALICE, BOB, DATABASE_URL

import db as vdb
import week


@pytest.fixture(scope="module", autouse=True)
def schema():
    vdb.init_db()
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
        cur.execute("alter table week_marks no force row level security")
        cur.execute("delete from week_marks")
        cur.execute("alter table week_marks force row level security")
    conn.close()


KEY = "canvas:17471:mi:862001"


def tick(uid, done):
    conn = vdb.get_db(user_id=uid)
    week.set_mark(conn, KEY, done=done)
    conn.commit()
    conn.close()


def done_for(uid):
    conn = vdb.get_db(user_id=uid)
    rows = conn.execute("SELECT key, done FROM week_marks").fetchall()
    conn.close()
    return {r["key"]: r["done"] for r in rows}


def test_the_migration_ran():
    conn = psycopg.connect(DATABASE_URL)
    row = conn.execute("select 1 from schema_migrations where id = '011_week_marks'").fetchone()
    conn.close()
    assert row is not None


def test_two_students_in_one_course_keep_their_own_ticks():
    tick(ALICE, True)
    tick(BOB, False)
    assert done_for(ALICE) == {KEY: 1}
    assert done_for(BOB) == {KEY: 0}
    tick(BOB, True)
    tick(ALICE, False)
    assert done_for(ALICE) == {KEY: 0}
    assert done_for(BOB) == {KEY: 1}


def test_the_plan_table_migration_ran_and_is_per_account():
    conn = psycopg.connect(DATABASE_URL)
    row = conn.execute("select 1 from schema_migrations where id = '012_week_plan_items'").fetchone()
    forced = conn.execute("select relforcerowsecurity from pg_class where relname = 'week_plan_items'").fetchone()
    conn.close()
    assert row is not None
    assert forced == (True,)

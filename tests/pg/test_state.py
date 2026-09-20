"""`/api/state` against real Postgres, batched and still isolated.

The page reloads this after most saves, so it used to cost one query per class, per
assignment and per file: nearly three hundred round trips against a network database.
It now loads every child table in one query each, with an `IN (...)` list. Two things
have to hold, and neither can be shown against SQLite:

  - the batched SQL runs at all on Postgres, which `?` placeholders and an empty id
    list both have a way of breaking;
  - the batching does not widen what a request can see. One query asking for forty
    files at once is exactly the shape that would leak another account's rows if row
    level security were not doing its job.
"""
import time

import psycopg
import pytest

from conftest import ALICE, BOB, DATABASE_URL

import app as vesta_app
import db as vdb


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
        for t in ("item_files", "subtasks", "materials", "notes", "items",
                  "schedule_entries", "grade_categories", "syllabus_topics",
                  "file_folders", "note_folders", "classes", "semesters", "app_settings"):
            cur.execute(f"alter table {t} no force row level security")
            cur.execute(f"delete from {t}")
            cur.execute(f"alter table {t} force row level security")
    conn.close()


def signed_in(uid, email):
    """A client holding a session for one account. No Supabase call is involved."""
    vesta_app.app.config["TESTING"] = True
    c = vesta_app.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
        sess["email"] = email
        sess["exp"] = time.time() + 3600
    return c


def seed(client, prefix, classes=3, items_per_class=4, files_per_class=3):
    """A small term, made through the app's own routes."""
    made = []
    for n in range(classes):
        cid = client.post("/api/classes", json={
            "code": f"{prefix} {100+n}", "name": f"{prefix} course {n}", "color": "blue",
            "schedule": [{"day": 1, "start": "10:00", "end": "11:20", "location": "AQ 3003"}],
            "gradeCategories": [{"name": "Quizzes", "weight": 20}],
        }).get_json()["id"]
        # Grade categories are only saved on update, not on create.
        client.put(f"/api/classes/{cid}", json={
            "gradeCategories": [{"name": "Quizzes", "weight": 20}]})
        made.append(cid)
        for k in range(items_per_class):
            iid = client.post("/api/items", json={
                "classId": cid, "title": f"{prefix} assignment {k}", "type": "assignment",
                "dueDate": "2026-10-%02d" % (k + 1), "weight": 10,
                "subtasks": [{"title": "Read it"}, {"title": "Write it"}],
            }).get_json()["id"]
            client.post(f"/api/classes/{cid}/materials", json={
                "kind": "link", "title": f"{prefix} reading {k}",
                "url": "https://example.com/x",
            })
        for f in range(files_per_class):
            client.post(f"/api/classes/{cid}/materials", json={
                "kind": "link", "title": f"{prefix} slides {f}",
                "url": "https://example.com/s",
            })
        client.post(f"/api/classes/{cid}/notes", json={"title": f"{prefix} note", "body": "x"})
    return made


def test_state_loads_on_postgres_with_everything_hanging_off_it():
    """The whole batched shape, exercised: schedule, grades, files, notes, subtasks."""
    alice = signed_in(ALICE, "alice@example.com")
    seed(alice, "PHIL")
    body = alice.get("/api/state").get_json()
    assert len(body["classes"]) == 3
    assert len(body["items"]) == 12
    for c in body["classes"]:
        assert len(c["schedule"]) == 1
        assert len(c["gradeCategories"]) == 1
        assert len(c["materials"]) == 7          # one per assignment, plus three decks
        assert len(c["notesList"]) == 1
        assert len(c["fileFolders"]) >= 7        # the defaults every class gets
    for i in body["items"]:
        assert len(i["subtasks"]) == 2
        assert i["headstarts"] == []
        assert i["rubric"] is None


def test_an_empty_term_asks_for_no_child_rows_at_all():
    """`IN ()` is a syntax error in Postgres, so an empty id list must skip the query."""
    bob = signed_in(BOB, "bob@example.com")
    body = bob.get("/api/state").get_json()
    assert body["classes"] == []
    assert body["items"] == []
    assert body["unfiled"] == {"notesList": [], "materials": []}


def test_batching_does_not_widen_what_one_account_can_see():
    """The load-bearing one. Both accounts hold a term; each sees only its own rows.

    A batched query fetches every class's files in one statement, which is the exact
    shape that would return the other account's rows if the policies were not applied
    to this role.
    """
    alice = signed_in(ALICE, "alice@example.com")
    bob = signed_in(BOB, "bob@example.com")
    seed(alice, "PHIL")
    seed(bob, "CMPT")

    a = alice.get("/api/state").get_json()
    b = bob.get("/api/state").get_json()

    assert {c["code"].split()[0] for c in a["classes"]} == {"PHIL"}
    assert {c["code"].split()[0] for c in b["classes"]} == {"CMPT"}
    assert all(i["title"].startswith("PHIL") for i in a["items"])
    assert all(i["title"].startswith("CMPT") for i in b["items"])
    for c in a["classes"]:
        assert all(m["title"].startswith("PHIL") for m in c["materials"])
        assert all(n["title"].startswith("PHIL") for n in c["notesList"])
    for c in b["classes"]:
        assert all(m["title"].startswith("CMPT") for m in c["materials"])


def test_a_saved_assignment_comes_back_whole():
    """The page now redraws from this row instead of reloading the term, so it has to
    carry everything the old full reload did."""
    alice = signed_in(ALICE, "alice@example.com")
    cid = seed(alice, "PHIL", classes=1, items_per_class=1, files_per_class=0)[0]
    iid = alice.get("/api/state").get_json()["items"][0]["id"]

    row = alice.put(f"/api/items/{iid}", json={
        "title": "Essay, final draft", "status": "done", "score": 88,
        "subtasks": [{"title": "Outline", "done": True}],
    }).get_json()
    assert row["id"] == iid
    assert row["title"] == "Essay, final draft"
    assert row["status"] == "done"
    assert row["score"] == 88
    assert [s["title"] for s in row["subtasks"]] == ["Outline"]
    assert row["subtasks"][0]["done"] is True
    assert row["classId"] == cid

    created = alice.post("/api/items", json={"classId": cid, "title": "New quiz",
                                            "type": "quiz"}).get_json()
    assert created["title"] == "New quiz" and created["id"]


def test_a_saved_class_comes_back_whole():
    alice = signed_in(ALICE, "alice@example.com")
    cid = seed(alice, "PHIL", classes=1, items_per_class=0, files_per_class=1)[0]
    row = alice.put(f"/api/classes/{cid}", json={
        "code": "PHIL 110", "name": "Introduction to Logic", "color": "violet",
        "schedule": [{"day": 2, "start": "09:30", "end": "10:20", "location": "WMC 3210"}],
    }).get_json()
    assert row["id"] == cid
    assert row["name"] == "Introduction to Logic"
    assert row["schedule"][0]["day"] == 2
    assert len(row["materials"]) == 1
    assert len(row["fileFolders"]) >= 7


def test_a_class_moves_between_terms_with_everything_on_it():
    """A class in the wrong term used to have to be deleted and rebuilt.

    The point of the test is the children: `semester_for` promises every row's copy of
    semester_id matches its class's, so a half-moved class would show its assignments
    in one term and its files in another.
    """
    alice = signed_in(ALICE, "alice@example.com")
    cid = seed(alice, "PHIL", classes=1, items_per_class=2, files_per_class=1)[0]
    before = alice.get("/api/state").get_json()
    first_term = before["semester"]["id"]
    assert len(before["classes"]) == 1 and len(before["items"]) == 2

    made = alice.post("/api/semesters", json={"name": "Spring 2027"}).get_json()
    second = made.get("id") or made.get("semester", {}).get("id")
    assert second and second != first_term

    row = alice.put(f"/api/classes/{cid}/semester", json={"semesterId": second}).get_json()
    assert row["semesterId"] == second

    # the old term is empty, and switching to the new one finds all of it there
    alice.put("/api/semesters/active", json={"id": first_term})
    empty = alice.get("/api/state").get_json()
    assert empty["classes"] == [] and empty["items"] == []

    alice.put("/api/semesters/active", json={"id": second})
    after = alice.get("/api/state").get_json()
    assert len(after["classes"]) == 1
    assert len(after["items"]) == 2
    assert len(after["classes"][0]["materials"]) == 3
    assert len(after["classes"][0]["notesList"]) == 1


def test_moving_a_class_to_a_term_that_does_not_exist_is_refused():
    alice = signed_in(ALICE, "alice@example.com")
    cid = seed(alice, "PHIL", classes=1, items_per_class=0, files_per_class=0)[0]
    r = alice.put(f"/api/classes/{cid}/semester", json={"semesterId": "nope"})
    assert r.status_code == 404

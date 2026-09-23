"""Canvas against a real Postgres, as a real account, under row level security.

Every query the Canvas feature runs was first written and tested on SQLite, and
NEXT.md is plain about what that proves: nothing, for the deployment. This runs each
of them the way production does, as a non-superuser with RLS forced, so a syntax
Postgres rejects, a parameter it reads differently, or a row RLS hides fails here and
not on vesta.study.
"""
import uuid

import psycopg
import pytest

from conftest import ALICE, BOB, DATABASE_URL

import canvas_sync as sync
import db as vdb


COURSE = "55"


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
    # Close whatever a failed test left open first. An open transaction holds locks,
    # and the ALTER TABLE below would otherwise wait on them forever: the whole run
    # hangs instead of reporting the one failure.
    while _open:
        try:
            _open.pop().close()
        except Exception:
            pass
    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    with conn.cursor() as cur:
        for t in ("materials", "items", "grade_categories", "file_folders", "classes",
                  "app_settings"):
            cur.execute(f"alter table {t} no force row level security")
            cur.execute(f"delete from {t}")
            cur.execute(f"alter table {t} force row level security")
    conn.close()


_open = []


def as_user(uid):
    conn = vdb.get_db(user_id=uid)
    _open.append(conn)
    return conn


def make_class(uid, code="REM 388"):
    conn = as_user(uid)
    cid = str(uuid.uuid4())
    conn.execute("INSERT INTO classes (id, semester_id, code, name, created_at)"
                 " VALUES (?,?,?,?,?)",
                 (cid, vdb.active_semester_id(conn), code, "Wildlife", "2026-09-01"))
    conn.commit()
    conn.close()
    return cid


def connect(uid, class_id, items=None, files=None, categories=None, weighted=True):
    conn = as_user(uid)
    state = sync.load_state(conn)
    state.update(host="canvas.sfu.ca", token="tok-" + uid[:4])
    state.setdefault("courses", {})[COURSE] = {"classId": class_id}
    sync.save_state(conn, state)
    sync.save_snapshot(conn, COURSE, {
        "courseId": COURSE, "fetchedAt": sync._now(),
        "plan": {"items": items or [], "categories": categories or [],
                 "weightsFromCanvas": weighted},
        "files": files or []})
    conn.commit()
    conn.close()


def planned(**kw):
    base = {"canvasId": 100, "importKey": "canvas:100", "title": "Quiz 01",
            "type": "quiz", "dueDate": "2026-09-16", "dueTime": "11:20", "notes": "Read ch 1",
            "url": None, "points": 10, "categoryCanvasId": None, "weight": None,
            "score": 90.0, "countsForGrade": True, "rubric": None}
    base.update(kw)
    return base


def cfile(**kw):
    base = {"id": 500, "display_name": "Lecture 1.pdf", "size": 1024,
            "content-type": "application/pdf",
            "url": "https://canvas.sfu.ca/files/500/download", "_module": "Week 1"}
    base.update(kw)
    return base


def accept_everything(uid):
    conn = as_user(uid)
    state = sync.load_state(conn)
    rev = sync.build_review(conn, state)
    ids = [u["id"] for c in rev["classes"] for g in c["groups"] for u in g["changes"]]
    report, to_fetch = sync.apply_selection(conn, state, ids, [])
    conn.commit()
    sync.save_state(conn, state)
    conn.close()
    return report, to_fetch


# ---------------------------------------------------------------------------

def test_migration_010_added_the_column():
    conn = psycopg.connect(DATABASE_URL)
    cols = [r[0] for r in conn.execute(
        "select column_name from information_schema.columns where table_name='materials'")]
    conn.close()
    assert "import_key" in cols


def test_a_first_sync_applies_end_to_end_on_postgres():
    """Review, then apply: categories, assignments, a module folder, a file link."""
    cid = make_class(ALICE)
    connect(ALICE, cid,
            items=[planned(categoryCanvasId=1),
                   planned(canvasId=101, importKey="canvas:101", title="Quiz 02",
                           categoryCanvasId=1, score=None)],
            categories=[{"canvasId": 1, "name": "Quizzes", "weight": 10.0, "dropLowest": 1}],
            files=[cfile(), cfile(id=501, display_name="Lecture 2.pdf")])
    report, to_fetch = accept_everything(ALICE)
    assert report["stale"] == []
    assert report["applied"]["items"] == 2
    assert report["applied"]["files"] == 2
    assert report["applied"]["categories"] == 1
    assert len(to_fetch) == 2

    conn = as_user(ALICE)
    items = conn.execute("SELECT * FROM items WHERE class_id=? ORDER BY title", (cid,)).fetchall()
    assert [i["import_key"] for i in items] == ["canvas:100", "canvas:101"]
    assert items[0]["score"] == 90.0
    mats = conn.execute("SELECT * FROM materials WHERE class_id=?", (cid,)).fetchall()
    assert {m["import_key"] for m in mats} == {"canvas:file:500", "canvas:file:501"}
    assert {m["kind"] for m in mats} == {"link"}
    folders = conn.execute("SELECT name FROM file_folders WHERE class_id=? AND kind='custom'",
                           (cid,)).fetchall()
    assert [f["name"] for f in folders] == ["Week 1"]
    # the parameterised LIKE, which a literal % would have broken on the way through
    assert sync._known_file_ids(conn, cid) == {500, 501}
    # and the review agrees there is nothing left
    assert sync.build_review(conn, sync.load_state(conn))["count"] == 0
    conn.close()


def test_a_folder_he_already_has_is_found_case_insensitively():
    cid = make_class(ALICE)
    conn = as_user(ALICE)
    conn.execute("INSERT INTO file_folders (id, class_id, parent_id, name, kind, sort_order,"
                 " created_at) VALUES (?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()), cid, None, "week 1", "custom", 0, "2026-09-01"))
    conn.commit()
    fid = sync.folder_by_name(conn, cid, "Week 1")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS n FROM file_folders WHERE class_id=? AND kind='custom'",
                     (cid,)).fetchone()["n"]
    conn.close()
    assert fid and n == 1


def test_accepting_a_change_and_keeping_another_on_postgres():
    cid = make_class(ALICE)
    conn = as_user(ALICE)
    iid = str(uuid.uuid4())
    # With a description of his own, so Canvas's is not offered: this test is about a
    # moved date and a kept grade, and nothing else.
    conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, due_date, due_time,"
                 " status, score, import_key, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (iid, vdb.active_semester_id(conn), cid, "Quiz 01", "quiz", "2026-09-15",
                  "11:20", "done", 80.0, "canvas:100", "My reminder", "2026-09-01"))
    conn.commit()
    conn.close()
    connect(ALICE, cid, items=[planned()])

    conn = as_user(ALICE)
    state = sync.load_state(conn)
    rev = sync.build_review(conn, state)
    units = {u["kind"]: u["id"] for c in rev["classes"] for g in c["groups"] for u in g["changes"]}
    report, _ = sync.apply_selection(conn, state, [units["moved"]], [units["grade"]])
    conn.commit()
    sync.save_state(conn, state)
    row = conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    assert (row["due_date"], row["score"], row["status"]) == ("2026-09-16", 80.0, "done")
    assert sync.build_review(conn, sync.load_state(conn))["count"] == 0
    conn.close()


def test_a_class_from_a_course_and_the_archived_check_on_postgres():
    conn = as_user(ALICE)
    cid = sync._class_from_course(conn, {"course_code": "SD381 OL01",
                                         "name": "SD381 OL01 Building Sustainable Communities"})
    conn.commit()
    row = conn.execute("SELECT code, name, color FROM classes WHERE id=?", (cid,)).fetchone()
    assert (row["code"], row["name"]) == ("SD 381", "Building Sustainable Communities")
    assert row["color"] in sync.CLASS_PALETTE
    # the LEFT JOIN onto semesters
    assert sync.class_is_live(conn, cid) is True
    assert sync.class_is_live(conn, str(uuid.uuid4())) is False
    conn.close()


def test_one_account_never_sees_another_accounts_canvas():
    """The token, the snapshot and the review all live under row level security."""
    alice_class = make_class(ALICE)
    connect(ALICE, alice_class, items=[planned()])
    bob = as_user(BOB)
    state = sync.load_state(bob)
    assert state["token"] == ""
    assert state["courses"] == {}
    assert sync.load_snapshot(bob, COURSE) is None
    assert sync.build_review(bob, state)["count"] == 0
    bob.close()


def test_a_check_outside_a_request_writes_as_the_right_account(monkeypatch):
    """The background check has no request, so it must connect as the account it is
    checking, or RLS gives it nothing and it reports success having read nothing."""
    import canvas

    cid = make_class(ALICE)
    connect(ALICE, cid)

    class FakeClient:
        def __init__(self, host, token):
            assert token == "tok-" + ALICE[:4]

        def course(self, course_id):
            return {"id": course_id, "apply_assignment_group_weights": True}

        def assignment_groups(self, course_id):
            return [{"id": 1, "name": "Quizzes", "group_weight": 10, "assignments": [
                {"id": 100, "name": "Quiz 01", "points_possible": 10, "published": True}]}]

        def course_files(self, course_id, known=None):
            return []

    monkeypatch.setattr(canvas, "Client", FakeClient)
    report = sync.check_account(ALICE)
    assert report["checked"] == 1
    conn = as_user(ALICE)
    snap = sync.load_snapshot(conn, COURSE)
    assert [i["title"] for i in snap["plan"]["items"]] == ["Quiz 01"]
    assert sync.load_state(conn)["lastCheck"]
    conn.close()


def test_undo_runs_on_postgres_and_checks_every_dependent_table():
    """Undo asks each of the tables that can hang off an assignment or a file whether
    anything does. A table missing on Postgres would abort the whole transaction."""
    cid = make_class(ALICE)
    connect(ALICE, cid, items=[planned(categoryCanvasId=1),
                               planned(canvasId=101, importKey="canvas:101", title="Quiz 02")],
            categories=[{"canvasId": 1, "name": "Quizzes", "weight": 10.0, "dropLowest": 0}],
            files=[cfile()])
    conn = as_user(ALICE)
    state = sync.load_state(conn)
    rev = sync.build_review(conn, state)
    ids = [u["id"] for c in rev["classes"] for g in c["groups"] for u in g["changes"]]
    report, _ = sync.apply_selection(conn, state, ids, [])
    conn.commit()
    sync.save_state(conn, state)
    entry = report.pop("_undo")
    sync.record_undo(conn, entry)
    conn.commit()
    # one assignment gets a subtask, so undo has to leave it
    kept_id = conn.execute("SELECT id FROM items WHERE import_key='canvas:101'").fetchone()["id"]
    conn.execute("INSERT INTO subtasks (id, item_id, title, done) VALUES (?,?,?,0)",
                 (str(uuid.uuid4()), kept_id, "Outline"))
    conn.commit()

    state = sync.load_state(conn)
    result, _ = sync.undo_apply(conn, state, sync.load_undo(conn)[0])
    conn.commit()
    sync.save_state(conn, state)
    assert result["undone"]["items"] == 1 and result["undone"]["files"] == 1
    assert result["kept"] == [{"title": "Quiz 02", "why": "it has subtasks"}]
    left = conn.execute("SELECT import_key FROM items WHERE class_id=?", (cid,)).fetchall()
    assert [r["import_key"] for r in left] == ["canvas:101"]
    assert sync.build_review(conn, sync.load_state(conn))["count"] >= 2
    conn.close()


def test_an_empty_description_fills_in_on_postgres():
    cid = make_class(ALICE)
    conn = as_user(ALICE)
    iid = str(uuid.uuid4())
    conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, due_date, due_time,"
                 " status, score, import_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (iid, vdb.active_semester_id(conn), cid, "Quiz 01", "quiz", "2026-09-16",
                  "11:20", "todo", 90.0, "canvas:100", "2026-09-01"))
    conn.commit()
    conn.close()
    connect(ALICE, cid, items=[planned()])
    report, _ = accept_everything(ALICE)
    conn = as_user(ALICE)
    assert conn.execute("SELECT notes FROM items WHERE id=?", (iid,)).fetchone()["notes"] == "Read ch 1"
    conn.close()


def test_merging_a_duplicate_and_undoing_it_on_postgres():
    """His SD 381 case: undo re-creates Canvas's copy and its category from a snapshot,
    which is exactly the kind of write where Postgres and SQLite differ."""
    cid = make_class(ALICE, code="SD 381")
    conn = as_user(ALICE)
    sem = vdb.active_semester_id(conn)
    mine_cat, canvas_cat, mine, copy = (str(uuid.uuid4()) for _ in range(4))
    for c, name, w in ((mine_cat, "Low-stakes weekly assignments", 35.0),
                       (canvas_cat, "Low-stakes module 1", 8.0)):
        conn.execute("INSERT INTO grade_categories (id, class_id, name, weight, drop_lowest,"
                     " sort_order, created_at) VALUES (?,?,?,?,?,?,?)", (c, cid, name, w, 0, 0, "2026-09-01"))
    conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, due_date, status,"
                 " score, category_id, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (mine, sem, cid, "Reading quiz (Week 1)", "quiz", "2026-09-13", "done", 60.0,
                  mine_cat, "mine", "2026-09-01"))
    conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, due_date, due_time,"
                 " status, score, category_id, import_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (copy, sem, cid, "Week 1 Readings Quiz", "quiz", "2026-09-13", "23:59", "todo", 60.0,
                  canvas_cat, "canvas:100", "2026-09-20"))
    conn.commit()
    conn.close()
    connect(ALICE, cid, items=[planned(title="Week 1 Readings Quiz", dueDate="2026-09-13",
                                       dueTime="23:59", categoryCanvasId=1, score=60.0)],
            categories=[{"canvasId": 1, "name": "Low-stakes module 1", "weight": 8.0, "dropLowest": 0}])

    conn = as_user(ALICE)
    state = sync.load_state(conn)
    rev = sync.build_review(conn, state)
    twin = [u for c in rev["classes"] for g in c["groups"] for u in g["changes"] if u["kind"] == "twin"]
    assert len(twin) == 1
    report, _ = sync.apply_selection(conn, state, [twin[0]["id"]], [])
    conn.commit()
    sync.save_state(conn, state)
    sync.record_undo(conn, report.pop("_undo"))
    conn.commit()
    assert report["applied"]["merged"] == 1
    assert conn.execute("SELECT import_key FROM items WHERE id=?", (mine,)).fetchone()["import_key"] == "canvas:100"
    assert conn.execute("SELECT 1 FROM items WHERE id=?", (copy,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM grade_categories WHERE id=?", (canvas_cat,)).fetchone() is None

    state = sync.load_state(conn)
    sync.undo_apply(conn, state, sync.load_undo(conn)[0])
    conn.commit()
    back = conn.execute("SELECT title, category_id FROM items WHERE id=?", (copy,)).fetchone()
    assert (back["title"], back["category_id"]) == ("Week 1 Readings Quiz", canvas_cat)
    assert conn.execute("SELECT 1 FROM grade_categories WHERE id=?", (canvas_cat,)).fetchone()
    assert conn.execute("SELECT import_key FROM items WHERE id=?", (mine,)).fetchone()["import_key"] is None
    conn.close()

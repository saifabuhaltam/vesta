"""This Week, assembled: module rows matched to his assignments, START cues, carried
rows, and the ticks that survive a re-check of Canvas.

The pure tests call `week.assemble_class` directly. The rest run against a real SQLite
database and the routes, because the point of them is what is stored and what comes
back.
"""
import json
import time
import uuid
from datetime import date

import pytest

import app as vesta_app
import canvas_sync
import db
import week

TERM = date(2026, 9, 9)
CLS = {"id": "c1", "code": "REM 388", "name": "Wildlife", "color": "#123456"}


def canvas_row(**kw):
    base = {"key": "canvas:15792:mi:1", "week": 3, "moduleWeek": 3, "title": "Quiz 02",
            "kind": "quiz", "task": True, "canvasType": "Quiz", "contentId": 900,
            "url": "https://canvas/x", "dueDate": "2026-09-22", "dueTime": "23:59",
            "section": "", "canvasDone": False}
    base.update(kw)
    return base


def course(*rows, weeks=None):
    return {"offset": 0, "offsetSource": "none", "weeks": weeks or {}, "entries": list(rows)}


def item(**kw):
    base = {"id": "i1", "class_id": "c1", "title": "Quiz 02", "type": "quiz",
            "due_date": "2026-09-22", "due_time": "23:59", "status": "todo",
            "weight": None, "category_id": None, "import_key": "canvas:555"}
    base.update(kw)
    return base


def assemble(week_no=3, today_week=3, **kw):
    return week.assemble_class(CLS, TERM, week_no, today_week, **kw)


# ---------------------------------------------------------------------------
# matching module rows to his assignments
# ---------------------------------------------------------------------------

def test_a_module_quiz_is_his_assignment_through_the_plan():
    plan = {"items": [{"canvasId": 555, "importKey": "canvas:555", "quizId": 900}]}
    out = assemble(course=course(canvas_row()), plan=plan, items=[item(status="done")])
    [row] = out["tasks"]
    assert row["itemId"] == "i1"
    assert row["done"] is True
    # and it is not listed a second time as an assignment due this week
    assert len(out["tasks"]) == 1


def test_his_due_date_wins_over_canvas():
    plan = {"items": [{"canvasId": 555, "importKey": "canvas:555", "quizId": 900}]}
    out = assemble(week_no=4, course=course(canvas_row()), plan=plan,
                   items=[item(due_date="2026-09-29")])
    assert [r["title"] for r in out["tasks"]] == ["Quiz 02"]
    assert out["tasks"][0]["dueDate"] == "2026-09-29"


def test_a_module_item_he_typed_himself_is_matched_by_title():
    out = assemble(course=course(canvas_row(title="Discussion 02 - Tree of Life",
                                            canvasType="Discussion", kind="discussion")),
                   items=[item(title="Discussion 02 - Tree of Life", import_key=None,
                               type="discussion")])
    assert len(out["tasks"]) == 1 and out["tasks"][0]["itemId"] == "i1"


def test_a_discussion_is_matched_by_canvas_full_title_not_the_shortened_one():
    # SD 381: the module row shows "Part 3b Activity", the assignment is called
    # "MODULE 1, Week 1, Part 3b Activity". Both are the same discussion.
    full = "MODULE 1, Week 1, Part 3b Activity- share your results"
    out = assemble(course=course(canvas_row(title="Part 3b Activity- share your results",
                                            rawTitle=full, canvasType="Discussion",
                                            kind="discussion", contentId=256463)),
                   items=[item(title=full, import_key="canvas:207953", type="discussion")])
    assert len(out["tasks"]) == 1 and out["tasks"][0]["itemId"] == "i1"


def test_an_assignment_no_module_mentions_still_shows_in_its_week():
    out = assemble(course=course(), items=[item(title="Essay 1", type="assignment",
                                                import_key=None)])
    assert [(r["title"], r["source"]) for r in out["tasks"]] == [("Essay 1", "assignment")]


def test_a_class_without_canvas_still_gets_its_assignments():
    out = assemble(course=None, items=[item(title="Problem set 3", import_key=None)])
    assert out["connected"] is False
    assert [r["title"] for r in out["tasks"]] == ["Problem set 3"]


# ---------------------------------------------------------------------------
# ticks
# ---------------------------------------------------------------------------

def test_a_stored_tick_beats_canvas_completion_and_canvas_completion_is_the_default():
    rows = course(canvas_row(key="canvas:1:mi:1", canvasType="Page", kind="work",
                             canvasDone=True, dueDate=None),
                  canvas_row(key="canvas:1:mi:2", canvasType="Page", kind="work",
                             canvasDone=True, dueDate=None, title="Part 2"))
    out = assemble(course=rows, marks={"canvas:1:mi:2": {"done": 0, "as_task": None}})
    done = {r["key"]: r["done"] for r in out["tasks"]}
    assert done == {"canvas:1:mi:1": True, "canvas:1:mi:2": False}


def test_a_row_switched_to_a_link_stays_a_link():
    out = assemble(course=course(canvas_row(canvasType="Page", kind="work", dueDate=None)),
                   marks={"canvas:15792:mi:1": {"done": 0, "as_task": 0}})
    assert out["tasks"] == [] and len(out["links"]) == 1


# ---------------------------------------------------------------------------
# START cues
# ---------------------------------------------------------------------------

def test_an_exam_gets_a_start_cue_ten_days_out():
    midterm = item(id="m1", title="Midterm Exam", type="exam", due_date="2026-10-19",
                   import_key=None)
    out = assemble(week_no=5, today_week=5, course=course(), items=[midterm])
    cue = [r for r in out["tasks"] if r["source"] == "cue"]
    assert [c["title"] for c in cue] == ["Start studying for Midterm Exam"]
    assert cue[0]["date"] == "2026-10-09"
    assert cue[0]["key"] == "cue:m1"


def test_a_heavy_assignment_gets_a_cue_and_a_light_one_does_not():
    heavy = item(id="a1", title="Research essay", type="assignment", weight=25,
                 due_date="2026-11-20", import_key=None)
    light = item(id="a2", title="Reading response", type="assignment", weight=2,
                 due_date="2026-11-20", import_key=None)
    rows = []
    for n in range(1, 16):
        rows += assemble(week_no=n, today_week=None, course=course(),
                         items=[heavy, light])["tasks"]
    cues = [r["title"] for r in rows if r["source"] == "cue"]
    assert cues == ["Start Research essay"]


def test_a_category_weight_is_shared_among_its_items():
    items = [item(id="q%d" % n, title="Quiz %d" % n, category_id="cat", weight=None,
                  import_key=None) for n in range(4)]
    weights = week.effective_weights(items, [{"id": "cat", "weight": 20}])
    assert weights["q0"] == 5.0


def test_a_finished_assignment_has_no_cue():
    done = item(id="m1", title="Midterm", type="exam", due_date="2026-10-19",
                status="done")
    assert week.cue_for(done, None) is None


# ---------------------------------------------------------------------------
# carried from earlier weeks
# ---------------------------------------------------------------------------

def test_unticked_work_from_earlier_weeks_is_carried_into_this_week_only():
    rows = course(canvas_row(key="canvas:1:mi:7", week=2, kind="reading",
                             canvasType="File", title="Old reading", dueDate=None),
                  canvas_row(key="canvas:1:mi:8", week=2, kind="link", task=False,
                             canvasType="File", title="Old slides", dueDate=None))
    now = assemble(week_no=3, today_week=3, course=rows)
    assert [r["title"] for r in now["carried"]] == ["Old reading"]
    later = assemble(week_no=4, today_week=3, course=rows)
    assert later["carried"] == []


# ---------------------------------------------------------------------------
# against the database and the routes
# ---------------------------------------------------------------------------

FIXTURE = json.load(open(__import__("os").path.join(
    __import__("os").path.dirname(__file__), "fixtures", "modules_fall2026.json")))


@pytest.fixture
def conn():
    db.init_db()
    c = db.get_db()
    yield c
    c.close()


@pytest.fixture
def psyc(conn):
    """PSYC 300W, mapped to its Canvas course, with a snapshot holding its modules."""
    sem = db.active_semester(conn)
    conn.execute("UPDATE semesters SET start_date=? WHERE id=?", ("2026-09-09", sem["id"]))
    cid = str(uuid.uuid4())
    conn.execute("INSERT INTO classes (id, semester_id, code, name, created_at)"
                 " VALUES (?,?,?,?,?)", (cid, sem["id"], "PSYC 300W", "Writing", "2026-09-01"))
    conn.commit()
    state = canvas_sync.load_state(conn)
    state["courses"] = {"17471": {"classId": cid}}
    canvas_sync.save_state(conn, state)
    canvas_sync.save_snapshot(conn, "17471", {
        "courseId": 17471, "timeZone": "America/Los_Angeles", "plan": {"items": []},
        "modules": FIXTURE["PSYC300W"]})
    yield cid
    conn.execute("DELETE FROM week_marks")
    conn.execute("DELETE FROM items WHERE class_id=?", (cid,))
    conn.execute("DELETE FROM classes WHERE id=?", (cid,))
    conn.execute("DELETE FROM app_settings WHERE key LIKE ?", ("canvas%",))
    conn.commit()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("INVITE_EMAILS", raising=False)
    vesta_app.app.config["TESTING"] = True
    with vesta_app.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = "local"
            s["exp"] = time.time() + 3600
        yield c


def psyc_of(body, cid):
    return [c for c in body["classes"] if c["classId"] == cid][0]


def test_the_week_route_builds_psyc_week_three(client, psyc):
    r = client.get("/api/week?start=2026-09-24&today=2026-09-24")
    assert r.status_code == 200
    body = r.get_json()
    assert (body["week"], body["weekStart"], body["weekEnd"]) == (3, "2026-09-21", "2026-09-27")
    p = psyc_of(body, psyc)
    assert p["topic"] == "Our Changing Relationship with the Written Word"
    assert "The End of Reading Is Here - The Atlantic" in [t["title"] for t in p["tasks"]]
    assert "PSYC 300W - Lecture 2 - The Written Word" in [t["title"] for t in p["links"]]


def test_a_tick_survives_and_can_be_undone(client, psyc):
    p = psyc_of(client.get("/api/week?start=2026-09-21").get_json(), psyc)
    reading = [t for t in p["tasks"] if t["kind"] == "reading"][0]
    r = client.post("/api/week/marks", json={"marks": [
        {"key": reading["key"], "classId": psyc, "done": True}]})
    assert r.status_code == 200
    p = psyc_of(client.get("/api/week?start=2026-09-21").get_json(), psyc)
    assert [t["done"] for t in p["tasks"] if t["key"] == reading["key"]] == [True]
    assert p["done"] == 1
    client.post("/api/week/marks", json={"marks": [{"key": reading["key"], "done": False}]})
    p = psyc_of(client.get("/api/week?start=2026-09-21").get_json(), psyc)
    assert p["done"] == 0


def test_switching_a_row_to_a_link_and_back(client, psyc):
    p = psyc_of(client.get("/api/week?start=2026-09-21").get_json(), psyc)
    key = p["tasks"][0]["key"]
    client.post("/api/week/marks", json={"marks": [{"key": key, "asTask": False}]})
    p = psyc_of(client.get("/api/week?start=2026-09-21").get_json(), psyc)
    assert key in [l["key"] for l in p["links"]]
    client.post("/api/week/marks", json={"marks": [{"key": key, "asTask": None}]})
    p = psyc_of(client.get("/api/week?start=2026-09-21").get_json(), psyc)
    assert key in [t["key"] for t in p["tasks"]]


def test_a_mark_for_an_assignment_is_refused(client, psyc):
    r = client.post("/api/week/marks", json={"marks": [{"key": "item:abc", "done": True}]})
    assert r.status_code == 400


def test_a_hand_set_shift_moves_the_course(client, psyc):
    r = client.post("/api/week/shift", json={"classId": psyc, "shift": 0})
    assert r.status_code == 200
    p = psyc_of(client.get("/api/week?start=2026-09-21").get_json(), psyc)
    assert p["offsetSource"] == "set"
    assert p["topic"] == "Reading Day 1"
    client.post("/api/week/shift", json={"classId": psyc, "shift": None})
    p = psyc_of(client.get("/api/week?start=2026-09-21").get_json(), psyc)
    assert p["offsetSource"] == "written"


def test_carried_rows_appear_only_in_the_current_week(client, psyc):
    now = psyc_of(client.get("/api/week?start=2026-09-21&today=2026-09-24").get_json(), psyc)
    assert any(c["title"].startswith("Writing is thinking") for c in now["carried"])
    ahead = psyc_of(client.get("/api/week?start=2026-09-28&today=2026-09-24").get_json(), psyc)
    assert ahead["carried"] == []


def test_marks_are_in_the_export():
    import prefs
    assert "week_marks" in prefs.EXPORT_TABLES

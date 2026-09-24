"""This Week from a course's own schedule, found and read automatically.

The real documents are pinned: PHIL 110's syllabus text, and the model's reading of IAT
201's progression map (tests/fixtures/plan_iat201_draft.json). No test calls the model;
`plan_class` is given a stand-in reader that counts its calls, because what matters
most here is when Vesta pays to read and when it does not.
"""
import json
import os
import time
import uuid
from datetime import date

import pytest

import ai
import app as vesta_app
import db
import ics
import week
import week_plan

HERE = os.path.join(os.path.dirname(__file__), "fixtures")
TERM = date(2026, 9, 9)
TERM_ROW = {"name": "Fall 2026", "start_date": "2026-09-09", "end_date": "2026-12-24"}
TZ = ics.zone("America/Los_Angeles")
CLS = {"id": "iat", "code": "IAT 201", "name": "HCI", "color": "#E0693C"}

with open(os.path.join(HERE, "phil110_syllabus.txt")) as _f:
    PHIL_SYLLABUS = _f.read()
TEXTBOOK = ("forall x: Calgary. An Introduction to Formal Logic. " * 40
            + "Chapter 1 Key notions. Chapter 2 First steps to symbolization. "
            + "Revised May 2026. First published 2005. ") * 20


@pytest.fixture(scope="module")
def draft():
    with open(os.path.join(HERE, "plan_iat201_draft.json")) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def modules():
    with open(os.path.join(HERE, "modules_fall2026.json")) as f:
        return json.load(f)["IAT201"]


def plan_items(draft):
    return [{"id": "p%d" % n, "class_id": "iat", "day": it["day"], "kind": it["kind"],
             "title": it["title"], "detail": it["detail"], "source": draft["sourceTitle"]}
            for n, it in enumerate(draft["items"])]


def assemble(draft, modules, week_no, items=(), marks=None, with_plan=True):
    course = week.course_entries("17051", modules, TERM, TZ)
    return week.assemble_class(CLS, TERM, week_no, week_no, course=course, items=items,
                               marks=marks, plan_items=plan_items(draft) if with_plan else None)


def titles(rows):
    return [r["title"] for r in rows]


# ---------------------------------------------------------------------------
# is it a schedule? (free)
# ---------------------------------------------------------------------------

def test_phils_syllabus_is_a_schedule_and_its_textbook_is_not():
    assert week_plan.schedule_weeks(PHIL_SYLLABUS, TERM) >= 12
    assert week_plan.schedule_weeks(TEXTBOOK, TERM) == 0


def test_the_syllabus_is_chosen_over_the_textbook():
    chosen = week_plan.choose([
        {"key": "canvas-file:3071904", "title": "forallx_SFU_May_2026.pdf", "text": TEXTBOOK, "fingerprint": "a"},
        {"key": "canvas-file:3302250", "title": "PHIL 110 - Syllabus Fall 2026.pdf", "text": PHIL_SYLLABUS, "fingerprint": "b"},
    ], TERM)
    assert chosen["key"] == "canvas-file:3302250"


def test_nothing_is_chosen_when_nothing_is_a_schedule():
    assert week_plan.choose([{"key": "k", "title": "Syllabus", "text": TEXTBOOK,
                              "fingerprint": "a"}], TERM) is None


def test_a_file_already_scored_is_not_downloaded_again():
    downloads = []

    class Client:
        base = "https://canvas/api/v1"

        def _get(self, url, params=None):
            class R:
                def json(self_inner):
                    return {"syllabus_body": '<a href="/courses/1/files/77">Syllabus</a>'}
            return R()

        def file(self, fid):
            return {"id": int(fid), "display_name": "Syllabus.pdf", "size": 1000,
                    "modified_at": "2026-09-01", "url": "https://canvas/files/77/download"}

        def download(self, url, path, max_bytes=None):
            downloads.append(url)
            with open(path, "w") as f:
                f.write(PHIL_SYLLABUS)

    extract = lambda path, name: open(path).read()
    first = week_plan.canvas_candidates(Client(), 1, [], extract)
    doc = [c for c in first if c["key"] == "canvas-file:77"][0]
    assert len(downloads) == 1 and doc["text"]
    second = week_plan.canvas_candidates(Client(), 1, [], extract, {doc["fingerprint"]: 13})
    doc2 = [c for c in second if c["key"] == "canvas-file:77"][0]
    assert len(downloads) == 1 and doc2["text"] is None and doc2["weeks"] == 13
    assert doc2["load"]() == PHIL_SYLLABUS.strip() and len(downloads) == 2


def test_module_items_named_like_a_schedule_are_candidates():
    mods = [{"name": "Course Overview", "items": [
        {"type": "File", "content_id": 5, "title": "iat-201-course-progression-map.docx"},
        {"type": "Page", "page_url": "iat-201-fall-2026-schedule", "title": "IAT 201 Fall 2026 Schedule"},
        {"type": "File", "content_id": 6, "title": "Designing with the Mind in Mind.pdf"}]}]
    keys = [r["key"] for r in week_plan.canvas_refs(mods, "")]
    assert keys == ["file:5", "page:iat-201-fall-2026-schedule"]


def test_files_linked_from_a_syllabus_page_are_found_once_each():
    html = ('<a href="https://canvas.sfu.ca/courses/17316/files/3302250?wrap=1">Syllabus</a>'
            '<img src="https://canvas.sfu.ca/courses/17316/files/3071872/preview">'
            '<a data-api-endpoint="https://canvas.sfu.ca/api/v1/courses/17316/files/3302250">again</a>')
    assert week_plan.syllabus_file_ids(html) == ["3302250", "3071872"]


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

def test_a_week_only_item_is_placed_and_one_with_no_week_is_dropped():
    items = week_plan.items_from({"items": [
        {"date": "", "week": 3, "kind": "reading", "title": "Read Ch. 3", "detail": ""},
        {"date": "2026-10-05", "week": 0, "kind": "lab", "title": "Lab 1", "detail": ""},
        {"date": "", "week": 0, "kind": "work", "title": "Somewhere, sometime", "detail": ""},
        {"date": "2026-10-05", "week": 0, "kind": "reading", "title": "  ", "detail": ""},
    ]}, TERM)
    assert [(i["day"], i["kind"], i["title"]) for i in items] == [
        ("2026-09-21", "reading", "Read Ch. 3"), ("2026-10-05", "work", "Lab 1")]


def test_row_ids_follow_what_the_row_says():
    a = {"day": "2026-09-21", "kind": "reading", "title": "Read Ch. 2"}
    assert week_plan.row_id("c", a) == week_plan.row_id("c", dict(a, title="read ch 2"))
    assert week_plan.row_id("c", a) != week_plan.row_id("c", dict(a, day="2026-09-28"))


# ---------------------------------------------------------------------------
# when Vesta pays to read
# ---------------------------------------------------------------------------

class Conn:
    """A connection that also knows which class the test made."""

    def __init__(self, c, class_id):
        self._c, self.class_id = c, class_id

    def __getattr__(self, name):
        return getattr(self._c, name)


@pytest.fixture
def conn():
    db.init_db()
    c = db.get_db()
    c.execute("UPDATE semesters SET start_date=?, end_date=? WHERE id=?",
              ("2026-09-09", "2026-12-24", db.active_semester_id(c)))
    cid = str(uuid.uuid4())
    c.execute("INSERT INTO classes (id, semester_id, code, name, created_at) VALUES (?,?,?,?,?)",
              (cid, db.active_semester_id(c), "PHIL 110", "Logic", "2026-09-01"))
    c.commit()
    yield Conn(c, cid)
    c.execute("DELETE FROM week_plan_items WHERE class_id=?", (cid,))
    c.execute("DELETE FROM week_marks")
    c.execute("DELETE FROM app_settings WHERE key LIKE ?", ("week_plan_auto:%",))
    c.execute("DELETE FROM classes WHERE id=?", (cid,))
    c.commit()
    c.close()


class Reader:
    """Stands in for the model. Returns PHIL 110's first weeks, and counts calls."""

    def __init__(self, items=None, refuse=None):
        self.calls = 0
        self.items = items if items is not None else [
            {"date": "2026-09-14", "week": 1, "kind": "reading", "title": "Read Ch. 1: What is logic?", "detail": ""},
            {"date": "2026-09-21", "week": 2, "kind": "reading", "title": "Read Ch. 2: Atomic sentences", "detail": ""},
            {"date": "2026-09-21", "week": 2, "kind": "topic", "title": "Truth-functional logic", "detail": ""},
        ]
        self.refuse = refuse

    def __call__(self, conn, text, title, term, confirmed):
        self.calls += 1
        if self.refuse:
            raise ai.AiRefused({"error": self.refuse}, 402)
        return {"items": self.items}, {"costUsd": 0.02}


def syllabus(fp="v1"):
    return [{"key": "canvas-file:3302250", "title": "PHIL 110 - Syllabus Fall 2026.pdf",
             "text": PHIL_SYLLABUS, "fingerprint": fp},
            {"key": "canvas-file:3071904", "title": "forallx.pdf", "text": TEXTBOOK, "fingerprint": "tb"}]


def plan_titles(conn):
    return [r["title"] for r in week_plan.plan_rows(conn, [conn.class_id]).get(conn.class_id, [])]


def test_a_schedule_is_read_once_and_not_again_until_it_changes(conn):
    read = Reader()
    assert week_plan.plan_class(conn, conn.class_id, syllabus(), TERM_ROW, read) == "read"
    assert read.calls == 1
    assert plan_titles(conn) == ["Read Ch. 1: What is logic?", "Read Ch. 2: Atomic sentences",
                                 "Truth-functional logic"]
    assert week_plan.plan_class(conn, conn.class_id, syllabus(), TERM_ROW, read) == "unchanged"
    assert read.calls == 1
    assert week_plan.plan_class(conn, conn.class_id, syllabus("v2"), TERM_ROW, read) == "read"
    assert read.calls == 2


def test_an_updated_schedule_keeps_the_ticks_on_rows_that_did_not_change(conn):
    week_plan.plan_class(conn, conn.class_id, syllabus(), TERM_ROW, Reader())
    ids = {r["title"]: r["id"] for r in week_plan.plan_rows(conn, [conn.class_id])[conn.class_id]}
    week.set_mark(conn, "plan:" + ids["Read Ch. 1: What is logic?"], done=True)
    week.set_mark(conn, "plan:" + ids["Read Ch. 2: Atomic sentences"], done=True)
    conn.commit()
    moved = Reader([
        {"date": "2026-09-14", "week": 1, "kind": "reading", "title": "Read Ch. 1: What is logic?", "detail": ""},
        {"date": "2026-09-28", "week": 3, "kind": "reading", "title": "Read Ch. 2: Atomic sentences", "detail": ""},
    ])
    week_plan.plan_class(conn, conn.class_id, syllabus("v2"), TERM_ROW, moved)
    marks = {r["key"] for r in conn.execute("SELECT key FROM week_marks WHERE done=1").fetchall()}
    assert "plan:" + ids["Read Ch. 1: What is logic?"] in marks
    assert "plan:" + ids["Read Ch. 2: Atomic sentences"] not in marks   # the row moved


def test_a_refused_read_is_recorded_and_tried_again_next_time(conn):
    assert week_plan.plan_class(conn, conn.class_id, syllabus(), TERM_ROW,
                                Reader(refuse="Reading this would go past today's AI limit.")) == "refused"
    state = week_plan.load_state(conn, conn.class_id)
    assert "AI limit" in state["error"]
    assert week_plan.plan_class(conn, conn.class_id, syllabus(), TERM_ROW, Reader()) == "read"


def test_a_document_with_nothing_weekly_in_it_is_not_paid_for_twice(conn):
    read = Reader(items=[])
    assert week_plan.plan_class(conn, conn.class_id, syllabus(), TERM_ROW, read) == "empty"
    assert week_plan.plan_class(conn, conn.class_id, syllabus(), TERM_ROW, read) == "unchanged"
    assert read.calls == 1


def test_no_schedule_means_no_read(conn):
    read = Reader()
    cands = [{"key": "k", "title": "forallx.pdf", "text": TEXTBOOK, "fingerprint": "tb"}]
    assert week_plan.plan_class(conn, conn.class_id, cands, TERM_ROW, read) == "none"
    assert read.calls == 0


def test_turned_off_means_no_read(conn):
    state = week_plan.load_state(conn, conn.class_id)
    state["off"] = True
    week_plan.save_state(conn, conn.class_id, state)
    read = Reader()
    assert week_plan.plan_class(conn, conn.class_id, syllabus(), TERM_ROW, read) == "off"
    assert read.calls == 0


def test_a_document_he_chose_is_used_even_if_another_scores_higher(conn):
    state = week_plan.load_state(conn, conn.class_id)
    state["pinned"] = "canvas-file:other"
    week_plan.save_state(conn, conn.class_id, state)
    cands = syllabus() + [{"key": "canvas-file:other", "title": "Reading list.pdf",
                           "text": "Week 1 Week 2 Week 3", "fingerprint": "o"}]
    week_plan.plan_class(conn, conn.class_id, cands, TERM_ROW, Reader())
    assert week_plan.load_state(conn, conn.class_id)["source"] == "canvas-file:other"


def test_a_class_off_canvas_is_planned_from_its_own_uploaded_syllabus(conn, monkeypatch):
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, class_id, title, filename, kind, extracted_text,"
                 " created_at) VALUES (?,?,?,?,?,?,?)",
                 (mid, conn.class_id, "PHIL 110 Syllabus", "syllabus.pdf", "file",
                  PHIL_SYLLABUS, "2026-09-01"))
    conn.commit()
    read = Reader()
    monkeypatch.setattr(week_plan, "read_plan", read)
    try:
        report = week_plan.auto_plan(None, class_ids=[conn.class_id])
    finally:
        conn.execute("DELETE FROM materials WHERE id=?", (mid,))
        conn.commit()
    assert report[conn.class_id] == "read"
    assert read.calls == 1


def test_nothing_is_spent_without_confirming():
    c = db.get_db()
    try:
        with pytest.raises(ai.AiRefused) as refused:
            week_plan.read_plan(c, PHIL_SYLLABUS, "Syllabus", TERM_ROW, confirmed=False,
                                client=object())
    finally:
        c.close()
    assert refused.value.status == 409


# ---------------------------------------------------------------------------
# the schedule merged with the modules
# ---------------------------------------------------------------------------

def test_the_map_and_canvas_name_one_reading_differently():
    assert week.title_match("Read Noba: Sensation and Perception, and Vision",
                            "Read Noba Sensation & Perception and Vision in Cognition for Designers") >= week.MATCH_AT
    assert week.title_match("Quiz 1", "Quiz 2") == 0.0


def test_one_reading_in_both_is_one_row_with_the_canvas_link(draft, modules):
    out = assemble(draft, modules, 3)
    tasks = titles(out["tasks"])
    assert "Read Designing with the Mind in Mind, ch. 1-3" in tasks
    assert "Read Designing With the Mind in Mind Ch 1-3" not in tasks
    row = [r for r in out["tasks"] if r["title"] == "Read Designing with the Mind in Mind, ch. 1-3"][0]
    assert "canvas.sfu.ca" in (row["url"] or "")
    assert row["alsoKeys"] and row["alsoKeys"][0].startswith("canvas:17051:mi:")
    assert out["topic"] == "Cognitive Neuroscience of Perception"


def test_module_work_the_schedule_does_not_list_is_kept(draft, modules):
    out = assemble(draft, modules, 3)
    assert "Muddy Points lecture 3" in titles(out["tasks"])
    assert "DDA1: Workflow Overview" in titles(out["links"])


def test_a_reading_canvas_puts_a_week_early_joins_the_schedules_week(draft, modules):
    # Canvas puts "Attention" in week 4; the map puts it in week 5.
    assert not any("Attention" in t for t in titles(assemble(draft, modules, 4)["tasks"]))
    assert "Read Noba: Attention, and Failures of Awareness" in titles(assemble(draft, modules, 5)["tasks"])


def test_a_canvas_tick_from_before_the_schedule_carries_over(draft, modules):
    course = week.course_entries("17051", modules, TERM, TZ)
    key = [e["key"] for e in course["entries"] if e["title"] == "Read Designing With the Mind in Mind Ch 1-3"][0]
    out = assemble(draft, modules, 3, marks={key: {"done": 1, "as_task": None}})
    row = [r for r in out["tasks"] if r["title"] == "Read Designing with the Mind in Mind, ch. 1-3"][0]
    assert row["done"] is True


def test_the_schedules_own_start_cues_replace_vestas(draft, modules):
    quiz = {"id": "q1", "class_id": "iat", "title": "Brain Quiz in sections", "type": "exam",
            "due_date": "2026-10-14", "due_time": None, "status": "todo", "weight": 15,
            "category_id": None, "import_key": None}
    rows = []
    for n in range(1, 16):
        rows += assemble(draft, modules, n, items=[quiz])["tasks"]
    assert not any(r["source"] == "cue" for r in rows)
    assert "Start studying for Quiz 1" in titles(rows)


def test_the_schedule_notes_a_holiday(draft, modules):
    assert any("Truth and Reconciliation" in n for n in assemble(draft, modules, 4)["notes"])


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("INVITE_EMAILS", raising=False)
    vesta_app.app.config["TESTING"] = True
    with vesta_app.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = "local"
            s["exp"] = time.time() + 3600
        yield c


def test_modules_only_hides_the_plan_and_turning_it_back_on_is_free(client, conn, monkeypatch):
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, class_id, title, filename, kind, extracted_text,"
                 " created_at) VALUES (?,?,?,?,?,?,?)",
                 (mid, conn.class_id, "Syllabus", "syllabus.pdf", "file", PHIL_SYLLABUS, "2026-09-01"))
    conn.commit()
    read = Reader()
    monkeypatch.setattr(week_plan, "read_plan", read)
    try:
        week_plan.plan_class(conn, conn.class_id, week_plan.material_candidates(conn, conn.class_id),
                             TERM_ROW, read)
        conn.commit()
        assert plan_titles(conn)
        assert client.post("/api/week/plan/%s/off" % conn.class_id, json={"off": True}).status_code == 200
        assert plan_titles(conn) == []
        got = client.get("/api/week/plan/%s?sources=1" % conn.class_id).get_json()
        assert got["state"]["off"] is True
        assert got["sources"][0]["key"] == "material:%s" % mid
        client.post("/api/week/plan/%s/off" % conn.class_id, json={"off": False})
        # Back at once, and the same document is not paid for again.
        assert plan_titles(conn) and read.calls == 1
    finally:
        conn.execute("DELETE FROM materials WHERE id=?", (mid,))
        conn.commit()


def test_choosing_a_document_that_is_gone_says_so(client, conn):
    r = client.post("/api/week/plan/%s/use" % conn.class_id, json={"source": "material:nope"})
    assert r.status_code == 404


def test_plan_items_are_in_the_export():
    import prefs
    assert "week_plan_items" in prefs.EXPORT_TABLES


def test_opening_this_week_plans_a_class_off_canvas_by_itself(client, conn, monkeypatch):
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, class_id, title, filename, kind, extracted_text,"
                 " created_at) VALUES (?,?,?,?,?,?,?)",
                 (mid, conn.class_id, "PHIL 110 Syllabus", "syllabus.pdf", "file",
                  PHIL_SYLLABUS, "2026-09-01"))
    conn.commit()
    read = Reader()
    monkeypatch.setattr(week_plan, "read_plan", read)
    try:
        body = client.get("/api/week?start=2026-09-21&today=2026-09-21").get_json()
        assert conn.class_id in body["planning"]
        for _ in range(50):
            if not week_plan._inflight:
                break
            time.sleep(0.05)
        body = client.get("/api/week?start=2026-09-21&today=2026-09-21").get_json()
        phil = [c for c in body["classes"] if c["classId"] == conn.class_id][0]
        assert body["planning"] == []
        assert titles(phil["tasks"]) == ["Read Ch. 2: Atomic sentences"]
        assert phil["plan"]["sourceTitle"] == "PHIL 110 Syllabus"
        assert read.calls == 1
    finally:
        conn.execute("DELETE FROM materials WHERE id=?", (mid,))
        conn.commit()

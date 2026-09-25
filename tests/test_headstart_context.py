"""What a Headstart chat is told about the assignment it is for.

Found by Saif on 2026-09-24: a chat on SD 381's "Assignment on reading (450 words / 3
paragraphs)" answered "I don't actually have the specific assignment prompt in front of
me" and wrote about the next week's reading. Chats were never given the assignment at
all, only the class's five newest files. These pin what they get now: the assignment,
the course's week around its due date, and the files for that week.
"""
import time
import uuid

import pytest

import ai
import app as vesta_app
import db
import week
import week_plan

MOORE = ("Read Moore et al., Vancouver's sustainability gap and lessons from the "
         "Southeast False Creek model sustainable community, Ch. 3")


class Conn:
    """A connection that also knows the class and assignment the test made."""

    def __init__(self, c, cid, iid):
        self._c, self.cid, self.iid = c, cid, iid

    def __getattr__(self, name):
        return getattr(self._c, name)


@pytest.fixture
def sd381():
    db.init_db()
    conn = db.get_db()
    sem = db.active_semester_id(conn)
    conn.execute("UPDATE semesters SET start_date=?, end_date=? WHERE id=?",
                 ("2026-09-09", "2026-12-24", sem))
    cid, iid = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute("INSERT INTO classes (id, semester_id, code, name, created_at) VALUES (?,?,?,?,?)",
                 (cid, sem, "SD 381", "Building Sustainable Communities", "2026-09-01"))
    conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, due_date, status,"
                 " created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (iid, sem, cid, "Assignment on reading (450 words / 3 paragraphs)", "assignment",
                  "2026-09-27", "todo", "2026-09-24"))
    week_plan.replace_plan(conn, cid, [
        {"day": "2026-09-21", "kind": "topic", "title": "Designing Sustainable Communities - Case Study", "detail": ""},
        {"day": "2026-09-21", "kind": "reading", "title": MOORE, "detail": ""},
        {"day": "2026-09-27", "kind": "assignment", "title": "Assignment on reading", "detail": "450 words/3 paragraphs"},
        {"day": "2026-09-27", "kind": "assignment", "title": "Submit your notes on videos in Parts 2 & 3", "detail": ""},
        {"day": "2026-09-28", "kind": "reading", "title": "Read Beatley, Biophilic urban design and planning", "detail": ""},
    ], "SD 381 List of weeks.pdf")
    conn.commit()
    yield Conn(conn, cid, iid)
    for sql in ("DELETE FROM week_plan_items WHERE class_id=?", "DELETE FROM materials WHERE class_id=?",
                "DELETE FROM threads WHERE class_id=?", "DELETE FROM items WHERE class_id=?",
                "DELETE FROM classes WHERE id=?"):
        conn.execute(sql, (cid,))
    conn.commit()
    conn.close()


@pytest.fixture
def conn(sd381):
    return sd381


def add_file(conn, title, text="Some text."):
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, class_id, title, filename, kind, extracted_text, created_at)"
                 " VALUES (?,?,?,?,?,?,?)", (mid, conn.cid, title, title, "file", text, "2026-09-01"))
    conn.commit()
    return mid


def test_a_chat_is_told_the_assignment_first(conn):
    context, used = ai.collect_sources(conn, {}, conn.cid, conn.iid, with_brief=True)
    assert used[0]["type"] == "assignment"
    assert context.startswith("--- assignment: Assignment on reading")
    assert "Due: 2026-09-27" in context


def test_the_week_it_is_due_brings_its_reading_and_not_next_weeks(conn):
    context, _ = ai.collect_sources(conn, {}, conn.cid, conn.iid, with_brief=True)
    assert "Moore et al." in context
    assert "Beatley" not in context
    assert "Designing Sustainable Communities - Case Study" in context


def test_the_assignment_is_not_listed_as_other_work_in_its_own_week(conn):
    brief, _, _ = ai.assignment_brief(conn, conn.iid)
    other = brief.split("Other work that week:")[1] if "Other work that week:" in brief else ""
    assert "Assignment on reading" not in other
    assert "Submit your notes on videos" in other


def test_no_instructions_is_said_rather_than_left_to_guess(conn):
    brief, _, _ = ai.assignment_brief(conn, conn.iid)
    assert "No instructions for this assignment are recorded" in brief
    conn.execute("UPDATE items SET notes=? WHERE id=?", ("Write 450 words on the Moore chapter.", conn.iid))
    conn.commit()
    brief, _, _ = ai.assignment_brief(conn, conn.iid)
    assert "Write 450 words on the Moore chapter." in brief
    assert "No instructions" not in brief


def test_pinned_sources_still_come_with_the_assignment(conn):
    mid = add_file(conn, "Lecture notes.pdf")
    _, used = ai.collect_sources(conn, {"materialIds": [mid]}, conn.cid, conn.iid, with_brief=True)
    assert [u["type"] for u in used] == ["assignment", "file"]


def test_the_one_shot_tools_are_not_given_the_brief_twice(conn):
    _, used = ai.collect_sources(conn, {}, conn.cid, conn.iid)
    assert not any(u["type"] == "assignment" for u in used)


def test_an_uploaded_copy_of_the_weeks_reading_is_read_before_newer_files(conn):
    moore = add_file(conn, "Moore et al 2019.pdf", "Southeast False Creek chapter text.")
    for n in range(6):
        add_file(conn, "Newer handout %d.pdf" % n)
    conn.execute("UPDATE materials SET created_at='2026-09-01' WHERE id=?", (moore,))
    conn.execute("UPDATE materials SET created_at='2026-09-20' WHERE id<>? AND class_id=?", (moore, conn.cid))
    conn.commit()
    _, used = ai.collect_sources(conn, {}, conn.cid, conn.iid, with_brief=True)
    assert used[1]["id"] == moore


def test_file_names_are_matched_to_readings_by_their_words():
    assert week.file_matches_reading("Moore 2019 Vancouver sustainability gap.pdf", MOORE)
    assert week.file_matches_reading("Moore et al 2019.pdf", MOORE)
    assert not week.file_matches_reading("Beatley 2011 Biophilic urban design.pdf", MOORE)
    assert not week.file_matches_reading("Meaningful citations_Fall 2026_Sept.pdf", MOORE)
    assert not week.file_matches_reading("SD 381 syllabus.pdf", MOORE)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("INVITE_EMAILS", raising=False)
    vesta_app.app.config["TESTING"] = True
    with vesta_app.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = "local"
            s["exp"] = time.time() + 3600
        yield c


def test_a_thread_says_it_reads_the_assignment(client, conn):
    r = client.post("/api/threads", json={"classId": conn.cid, "itemId": conn.iid,
                                          "title": "Assignment on reading"})
    tid = r.get_json()["id"]
    got = client.get("/api/threads/%s" % tid).get_json()
    assert got["sources"][0]["type"] == "assignment"


def test_a_chat_not_yet_written_can_preview_what_it_will_read(client, conn):
    mid = add_file(conn, "Moore et al 2019.pdf", "Chapter text.")
    r = client.post("/api/threads/preview", json={"classId": conn.cid, "itemId": conn.iid,
                                                  "selection": {"materialIds": [mid]}})
    body = r.get_json()
    assert [s["type"] for s in body["sources"]] == ["assignment", "file"]
    assert body["pinned"] == [{"type": "file", "id": mid, "title": "Moore et al 2019.pdf",
                               "readable": True}]


def test_a_new_chat_keeps_what_was_attached_before_its_first_message(client, conn):
    mid = add_file(conn, "Moore et al 2019.pdf", "Chapter text.")
    tid = client.post("/api/threads", json={"classId": conn.cid, "itemId": conn.iid, "title": "x",
                                            "selection": {"materialIds": [mid]}}).get_json()["id"]
    got = client.get("/api/threads/%s" % tid).get_json()
    assert got["selection"]["materialIds"] == [mid]

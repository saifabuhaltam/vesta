"""Canvas review: nothing reaches a class until he has seen it and said yes.

What is under test is Saif's design of 2026-09-22. A check stores what Canvas has and
touches nothing else; a review compares that against his classes as they are at that
moment; an accepted change is applied only if it is still exactly the change he saw;
and "keep mine" is remembered against the value Canvas offered, so the same offer is
not made twice and a genuinely new one still is.
"""
import os
import uuid

import pytest

import canvas
import canvas_sync as sync
import db


COURSE = "55"


@pytest.fixture
def conn():
    db.init_db()
    c = db.get_db()
    c.execute("DELETE FROM app_settings WHERE key LIKE ?", ("canvas%",))
    c.commit()
    yield c
    c.execute("DELETE FROM app_settings WHERE key LIKE ?", ("canvas%",))
    c.commit()
    c.close()


@pytest.fixture
def class_id(conn):
    cid = str(uuid.uuid4())
    conn.execute("INSERT INTO classes (id, semester_id, code, name, created_at)"
                 " VALUES (?,?,?,?,?)",
                 (cid, db.active_semester_id(conn), "REM388", "Wildlife", "2026-09-01"))
    conn.commit()
    yield cid
    for table in ("materials", "items", "grade_categories", "file_folders"):
        conn.execute("DELETE FROM %s WHERE class_id=?" % table, (cid,))
    conn.execute("DELETE FROM classes WHERE id=?", (cid,))
    conn.commit()


def planned(**kw):
    base = {"canvasId": 100, "importKey": "canvas:100", "title": "Quiz 01",
            "type": "quiz", "dueDate": "2026-09-16", "dueTime": "11:20", "notes": "",
            "url": None, "points": 10, "categoryCanvasId": None, "weight": 5.0,
            "score": None, "countsForGrade": True, "rubric": None}
    base.update(kw)
    return base


def cfile(**kw):
    base = {"id": 500, "display_name": "Lecture 1.pdf", "size": 1024,
            "content-type": "application/pdf",
            "url": "https://canvas.sfu.ca/files/500/download", "_module": "Week 1"}
    base.update(kw)
    return base


def connect(conn, class_id, items=None, files=None, weighted=True, categories=None):
    """A connected account, one mapped course, and a snapshot as a check would leave."""
    state = sync.load_state(conn)
    state.update(host="canvas.sfu.ca", token="tok")
    state.setdefault("courses", {})[COURSE] = {"classId": class_id}
    sync.save_state(conn, state)
    sync.save_snapshot(conn, COURSE, {
        "courseId": COURSE, "fetchedAt": sync._now(),
        "plan": {"items": items or [], "categories": categories or [],
                 "weightsFromCanvas": weighted},
        "files": files or []})
    conn.commit()
    return state


def add_item(conn, class_id, **kw):
    iid = str(uuid.uuid4())
    row = {"title": "Quiz 01", "type": "quiz", "due_date": None, "due_time": None,
           "weight": None, "score": None, "status": "todo", "import_key": None,
           "category_id": None}
    row.update(kw)
    conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, due_date,"
                 " due_time, weight, score, status, import_key, category_id, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (iid, db.active_semester_id(conn), class_id, row["title"], row["type"],
                  row["due_date"], row["due_time"], row["weight"], row["score"],
                  row["status"], row["import_key"], row["category_id"], "2026-09-01"))
    conn.commit()
    return iid


def review(conn):
    return sync.build_review(conn, sync.load_state(conn))


def units(rev, kind=None):
    return [u for c in rev["classes"] for g in c["groups"] for u in g["changes"]
            if kind is None or u["kind"] == kind]


def item_row(conn, iid):
    return conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()


def accept(conn, ids=(), reject=()):
    state = sync.load_state(conn)
    report, to_fetch = sync.apply_selection(conn, state, list(ids), list(reject))
    conn.commit()
    sync.save_state(conn, state)
    return report, to_fetch


# ---------------------------------------------------------------------------
# a check changes nothing
# ---------------------------------------------------------------------------

def test_a_review_shows_what_would_change_and_writes_nothing(conn, class_id):
    connect(conn, class_id, items=[planned()], files=[cfile()])
    rev = review(conn)
    assert rev["count"] == 2
    assert {u["kind"] for u in units(rev)} == {"newItem", "newFile"}
    assert conn.execute("SELECT COUNT(*) AS n FROM items WHERE class_id=?",
                        (class_id,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM materials WHERE class_id=?",
                        (class_id,)).fetchone()["n"] == 0


def test_a_check_stores_a_snapshot_and_nothing_else(conn, class_id, monkeypatch):
    connect(conn, class_id)

    class FakeClient:
        def __init__(self, host, token):
            pass

        def course(self, cid):
            return {"id": cid, "apply_assignment_group_weights": True}

        def assignment_groups(self, cid):
            return [{"id": 1, "name": "Quizzes", "group_weight": 10, "assignments": [
                {"id": 100, "name": "Quiz 01", "points_possible": 10, "published": True,
                 "due_at": "2026-09-16T18:20:00Z"},
                {"id": 101, "name": "Quiz 02", "points_possible": 10, "published": True}]}]

        def modules(self, cid):
            return []

        def course_files(self, cid, known=None, modules=None):
            return [cfile()]

    monkeypatch.setattr(canvas, "Client", FakeClient)
    report = sync.check_account(None)
    assert report["checked"] == 1
    snap = sync.load_snapshot(conn, COURSE)
    assert [i["title"] for i in snap["plan"]["items"]] == ["Quiz 01", "Quiz 02"]
    assert conn.execute("SELECT COUNT(*) AS n FROM items WHERE class_id=?",
                        (class_id,)).fetchone()["n"] == 0
    assert sync.load_state(conn)["lastError"] is None


def test_a_dead_token_is_recorded_where_the_page_can_show_it(conn, class_id, monkeypatch):
    """Nobody is watching a background check. The failure has to be kept."""
    connect(conn, class_id)

    class DeadClient:
        def __init__(self, host, token):
            pass

        def course(self, cid):
            raise canvas.CanvasError("Canvas rejected the access token.", 401,
                                     needs_token=True)

    monkeypatch.setattr(canvas, "Client", DeadClient)
    sync.check_account(None)
    state = sync.load_state(conn)
    assert "rejected the access token" in state["lastError"]
    assert "rejected" in state["courses"][COURSE]["lastError"]
    assert sync.public_state(state)["lastError"]


def test_a_check_does_not_overwrite_a_snooze_made_while_it_ran(conn, class_id, monkeypatch):
    connect(conn, class_id)

    class SlowClient:
        def __init__(self, host, token):
            pass

        def course(self, cid):
            # He presses Later while Canvas is still answering.
            c2 = db.get_db()
            s = sync.load_state(c2)
            sync.snooze(s, "abc")
            sync.save_state(c2, s)
            c2.close()
            return {"id": cid}

        def assignment_groups(self, cid):
            return []

        def modules(self, cid):
            return []

        def course_files(self, cid, known=None, modules=None):
            return []

    monkeypatch.setattr(canvas, "Client", SlowClient)
    sync.check_account(None)
    assert sync.load_state(conn)["snoozed"]["all"] == "abc"


def test_a_class_in_an_archived_term_is_not_checked_or_reviewed(conn, class_id):
    """A background job never passes the archived-term guard, so it asks itself."""
    connect(conn, class_id, items=[planned()])
    sid = str(uuid.uuid4())
    conn.execute("INSERT INTO semesters (id, name, status) VALUES (?,?,?)",
                 (sid, "Spring 2026", "archived"))
    conn.execute("UPDATE classes SET semester_id=? WHERE id=?", (sid, class_id))
    conn.commit()
    assert review(conn)["count"] == 0
    assert sync.class_is_live(conn, class_id) is False
    conn.execute("DELETE FROM semesters WHERE id=?", (sid,))
    conn.commit()


# ---------------------------------------------------------------------------
# grouping and the banner's words
# ---------------------------------------------------------------------------

def test_a_moved_deadline_is_one_change_and_is_named_in_the_banner(conn, class_id):
    add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-15",
             due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[planned(dueDate="2026-09-16", dueTime="13:20")])
    rev = review(conn)
    moved = units(rev, "moved")
    assert len(moved) == 1                      # date and time together, not two rows
    assert moved[0]["before"]["text"] == "Sep 15 11:20am"
    assert moved[0]["after"]["text"] == "Sep 16 1:20pm"
    assert rev["headlines"][0] == "REM388 Quiz 01 moved Sep 15 11:20am to Sep 16 1:20pm"


def test_deadlines_and_grades_come_before_new_things(conn, class_id):
    add_item(conn, class_id, title="Quiz 01", import_key="canvas:100",
             due_date="2026-09-15", due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[
        planned(dueDate="2026-09-16", score=90.0),
        planned(canvasId=101, importKey="canvas:101", title="Quiz 02")],
        files=[cfile()])
    kinds = [g["kind"] for g in review(conn)["groups"]]
    assert kinds == ["moved", "grade", "newItem", "newFile"]


def test_a_date_canvas_fills_into_an_empty_field_is_not_called_a_move(conn, class_id):
    add_item(conn, class_id, import_key="canvas:100", weight=5.0)
    connect(conn, class_id, items=[planned()])
    assert [u["kind"] for u in units(review(conn))] == ["dateAdded"]


def test_nothing_to_review_is_a_zero_not_an_error(conn, class_id):
    add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-16",
             due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[planned()])
    rev = review(conn)
    assert rev["count"] == 0
    assert rev["headlines"] == []
    assert rev["fingerprint"] is None


# ---------------------------------------------------------------------------
# accepting
# ---------------------------------------------------------------------------

def test_accepting_a_moved_deadline_moves_it_and_nothing_else(conn, class_id):
    iid = add_item(conn, class_id, title="My name for it", import_key="canvas:100",
                   due_date="2026-09-15", due_time="11:20", weight=25.0, status="done")
    connect(conn, class_id, items=[planned(title="My name for it", dueDate="2026-09-16",
                                           categoryCanvasId=1)],
            categories=[{"canvasId": 1, "name": "Quizzes", "weight": 10.0, "dropLowest": 0}])
    moved = units(review(conn), "moved")[0]
    report, _ = accept(conn, [moved["id"]])
    row = item_row(conn, iid)
    assert row["due_date"] == "2026-09-16"
    assert row["status"] == "done"
    assert row["weight"] == 25.0
    assert row["category_id"] is None           # not quietly moved into Canvas's category
    assert report["stale"] == []


def test_accepting_a_new_assignment_writes_it_with_its_category(conn, class_id):
    connect(conn, class_id, items=[planned(categoryCanvasId=1, weight=None)],
            categories=[{"canvasId": 1, "name": "Quizzes", "weight": 10.0, "dropLowest": 1}])
    new = units(review(conn), "newItem")[0]
    accept(conn, [new["id"]])
    row = conn.execute("SELECT * FROM items WHERE class_id=?", (class_id,)).fetchone()
    cat = conn.execute("SELECT * FROM grade_categories WHERE class_id=?", (class_id,)).fetchone()
    assert row["import_key"] == "canvas:100"
    assert row["category_id"] == cat["id"]
    assert (cat["weight"], cat["drop_lowest"]) == (10.0, 1)
    assert review(conn)["count"] == 0           # and the next review agrees it is done


def test_a_new_assignment_from_a_points_course_arrives_without_a_weight(conn, class_id):
    """IAT201's weights are all 0% and PSYC300W's first is 62.5%. Neither is written,
    and the next review does not then offer the weight it left out."""
    connect(conn, class_id, items=[planned(weight=62.5)], weighted=False)
    new = units(review(conn), "newItem")[0]
    assert new["after"]["weight"] is None
    accept(conn, [new["id"]])
    assert conn.execute("SELECT weight FROM items WHERE class_id=?",
                        (class_id,)).fetchone()["weight"] is None
    assert review(conn)["count"] == 0


def test_a_change_that_moved_since_he_looked_is_skipped_not_applied(conn, class_id):
    """He viewed the review, then edited the date in another tab. His edit stands."""
    iid = add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-15",
                   due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[planned(dueDate="2026-09-16")])
    seen_id = units(review(conn), "moved")[0]["id"]
    conn.execute("UPDATE items SET due_date=? WHERE id=?", ("2026-09-20", iid))
    conn.commit()
    report, _ = accept(conn, [seen_id])
    assert report["stale"] == [seen_id]
    assert item_row(conn, iid)["due_date"] == "2026-09-20"


def test_a_change_canvas_revised_since_he_looked_is_skipped_too(conn, class_id):
    iid = add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-15",
                   due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[planned(dueDate="2026-09-16")])
    seen_id = units(review(conn), "moved")[0]["id"]
    connect(conn, class_id, items=[planned(dueDate="2026-09-18")])   # a new check lands
    report, _ = accept(conn, [seen_id])
    assert report["stale"] == [seen_id]
    assert item_row(conn, iid)["due_date"] == "2026-09-15"


def test_accept_all_in_a_group_is_just_every_id_in_it(conn, class_id):
    connect(conn, class_id, items=[planned(canvasId=i, importKey="canvas:%d" % i,
                                           title="Reading %d" % i) for i in range(1, 26)])
    group = next(g for g in review(conn)["groups"] if g["kind"] == "newItem")
    assert group["count"] == 25
    accept(conn, [u["id"] for u in group["changes"]])
    assert conn.execute("SELECT COUNT(*) AS n FROM items WHERE class_id=?",
                        (class_id,)).fetchone()["n"] == 25


# ---------------------------------------------------------------------------
# keeping his own
# ---------------------------------------------------------------------------

def test_keep_mine_is_remembered_and_not_asked_again(conn, class_id):
    iid = add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-16",
                   due_time="11:20", weight=5.0, score=88.0)
    connect(conn, class_id, items=[planned(score=91.0)])
    grade = units(review(conn), "grade")[0]
    accept(conn, reject=[grade["id"]])
    assert item_row(conn, iid)["score"] == 88.0
    assert units(review(conn), "grade") == []


def test_keep_mine_does_not_silence_a_different_value_later(conn, class_id):
    """He kept 88 over Canvas's 91. When Canvas re-marks it to 94, that is news."""
    add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-16",
             due_time="11:20", weight=5.0, score=88.0)
    connect(conn, class_id, items=[planned(score=91.0)])
    accept(conn, reject=[units(review(conn), "grade")[0]["id"]])
    connect(conn, class_id, items=[planned(score=94.0)])
    again = units(review(conn), "grade")
    assert len(again) == 1
    assert again[0]["after"] == 94.0


def test_refusing_a_new_assignment_hides_it_and_show_hidden_brings_it_back(conn, class_id):
    connect(conn, class_id, items=[planned(title="Reading 1", type="reading")])
    new = units(review(conn), "newItem")[0]
    accept(conn, reject=[new["id"]])
    rev = review(conn)
    assert rev["count"] == 0
    assert rev["classes"][0]["hidden"] == 1

    state = sync.load_state(conn)
    for c in sync.build_review(conn, state)["classes"]:
        sync.forget(state, c["courseId"], c["_dismissedKeys"])
    sync.save_state(conn, state)
    assert units(review(conn), "newItem")[0]["title"] == "Reading 1"


def test_deleting_an_accepted_assignment_is_remembered_the_same_way(conn, class_id):
    connect(conn, class_id, items=[planned()])
    accept(conn, [units(review(conn), "newItem")[0]["id"]])
    conn.execute("DELETE FROM items WHERE class_id=?", (class_id,))
    conn.commit()
    rev = review(conn)
    assert rev["count"] == 0
    assert rev["classes"][0]["hidden"] == 1


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def test_accepting_a_new_file_lands_a_link_and_queues_the_small_ones(conn, class_id):
    connect(conn, class_id, files=[cfile(), cfile(id=501, display_name="Deck.pptx",
                                                  size=40 * 1024 * 1024)])
    ids = [u["id"] for u in units(review(conn), "newFile")]
    _, to_fetch = accept(conn, ids)
    assert conn.execute("SELECT COUNT(*) AS n FROM materials WHERE class_id=? AND kind='link'",
                        (class_id,)).fetchone()["n"] == 2
    assert [f["filename"] for f in to_fetch] == ["Lecture 1.pdf"]


def test_a_same_name_file_offers_to_add_canvas_copy_never_to_replace_his(conn, class_id):
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, semester_id, class_id, title, kind, filename,"
                 " stored_name, size, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (mid, db.active_semester_id(conn), class_id, "Lecture 1.pdf", "file",
                  "Lecture 1.pdf", "mine.pdf", 9999, "2026-09-01"))
    conn.commit()
    connect(conn, class_id, files=[cfile()])
    conflict = units(review(conn), "conflict")[0]
    assert conflict["keepLabel"] == "Keep mine"
    accept(conn, [conflict["id"]])
    rows = conn.execute("SELECT * FROM materials WHERE class_id=? ORDER BY created_at",
                        (class_id,)).fetchall()
    assert len(rows) == 2
    assert [r["stored_name"] for r in rows if r["id"] == mid] == ["mine.pdf"]


def test_a_file_the_professor_replaced_can_be_updated(conn, class_id):
    """Only for a file Canvas put there. It holds nothing of his."""
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, semester_id, class_id, title, kind, filename,"
                 " stored_name, size, import_key, url, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (mid, db.active_semester_id(conn), class_id, "Lecture 1.pdf", "file",
                  "Lecture 1.pdf", "old.pdf", 1024, "canvas:file:500",
                  "https://canvas.sfu.ca/files/500/download", "2026-09-01"))
    conn.commit()
    connect(conn, class_id, files=[cfile(size=2048)])
    upd = units(review(conn), "updatedFile")[0]
    assert upd["acceptLabel"] == "Update"
    _, to_fetch = accept(conn, [upd["id"]])
    assert to_fetch == [{"materialId": mid, "url": "https://canvas.sfu.ca/files/500/download",
                         "filename": "Lecture 1.pdf", "size": 2048, "replace": True}]


def test_a_known_file_read_as_a_stub_is_not_a_change(conn, class_id):
    """The fast daily path skips re-reading known files; a stub has no size."""
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, semester_id, class_id, title, kind, filename,"
                 " size, import_key, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (mid, db.active_semester_id(conn), class_id, "Lecture 1.pdf", "link",
                  "Lecture 1.pdf", 1024, "canvas:file:500", "2026-09-01"))
    conn.commit()
    connect(conn, class_id, files=[{"id": 500, "display_name": "Lecture 1", "size": None,
                                    "_module": "Week 1", "_stub": True}])
    assert review(conn)["count"] == 0


# ---------------------------------------------------------------------------
# later
# ---------------------------------------------------------------------------

def test_later_hides_the_banner_until_something_new_is_found(conn, class_id):
    connect(conn, class_id, items=[planned()])
    rev = review(conn)
    state = sync.load_state(conn)
    sync.snooze(state, rev["fingerprint"])
    sync.save_state(conn, state)
    assert review(conn)["snoozed"] is True

    connect(conn, class_id, items=[planned(), planned(canvasId=101, importKey="canvas:101",
                                                      title="Quiz 02")])
    assert review(conn)["snoozed"] is False


def test_later_on_one_class_leaves_the_banner_alone(conn, class_id):
    connect(conn, class_id, items=[planned()])
    rev = review(conn)
    state = sync.load_state(conn)
    sync.snooze(state, rev["classes"][0]["fingerprint"], class_id)
    sync.save_state(conn, state)
    rev = review(conn)
    assert rev["classes"][0]["snoozed"] is True
    assert rev["snoozed"] is False


# ---------------------------------------------------------------------------
# through the routes
# ---------------------------------------------------------------------------

from test_auth import FakeSupabase, supa, client, sign_in     # noqa: E402,F401


@pytest.fixture
def signed_in(client, supa):
    sign_in(client, supa)
    return client


def test_the_status_route_never_sends_the_token(signed_in, conn, class_id):
    connect(conn, class_id, items=[planned()])
    body = signed_in.get("/api/canvas").get_json()
    assert "tok" not in repr({k: v for k, v in body.items() if k != "review"})
    assert body["connected"] is True
    assert body["review"]["count"] == 1
    assert body["review"]["headlines"] == ["1 new assignment"]


def test_the_review_route_sends_no_private_fields(signed_in, conn, class_id):
    connect(conn, class_id, items=[planned()])
    body = signed_in.get("/api/canvas/review").get_json()
    assert "_item" not in repr(body)
    assert "_rejectKey" not in repr(body)


def test_accept_and_keep_through_the_route(signed_in, conn, class_id):
    iid = add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-15",
                   due_time="11:20", weight=5.0, score=80.0)
    connect(conn, class_id, items=[planned(score=95.0)])
    body = signed_in.get("/api/canvas/review").get_json()
    ids = {u["kind"]: u["id"] for g in body["groups"] for u in g["changes"]}
    out = signed_in.post("/api/canvas/apply",
                         json={"accept": [ids["moved"]], "reject": [ids["grade"]]}).get_json()
    assert out["stale"] == []
    assert out["review"]["count"] == 0
    row = item_row(conn, iid)
    assert (row["due_date"], row["score"]) == ("2026-09-16", 80.0)


def test_two_courses_cannot_be_paired_with_one_class(signed_in, conn, class_id):
    connect(conn, class_id)
    r = signed_in.put("/api/canvas/mapping", json={"mapping": {"1": class_id, "2": class_id}})
    assert r.status_code == 400


def test_opening_vesta_does_not_start_a_check_when_the_last_is_recent(signed_in, conn, class_id):
    state = connect(conn, class_id)
    state = sync.load_state(conn)
    state["lastCheck"] = sync._now()
    sync.save_state(conn, state)
    assert signed_in.post("/api/canvas/check", json={"ifStale": True}).get_json()["fresh"]


# ---------------------------------------------------------------------------
# fetching a file's bytes
# ---------------------------------------------------------------------------

def test_fetching_turns_the_link_into_a_file_once(conn, class_id, monkeypatch):
    import app as vesta_app

    connect(conn, class_id)
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, semester_id, class_id, title, kind, url, filename,"
                 " size, import_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (mid, db.active_semester_id(conn), class_id, "Notes.txt", "link",
                  "https://canvas.sfu.ca/files/9/download", "Notes.txt", 5,
                  "canvas:file:9", "2026-09-01"))
    conn.commit()
    calls = []

    def fake_download(self, url, dest, max_bytes=None):
        calls.append(url)
        with open(dest, "wb") as fh:
            fh.write(b"hello")
        return 5

    monkeypatch.setattr(canvas.Client, "download", fake_download)
    stored = vesta_app.fetch_canvas_material(mid)
    row = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    assert row["kind"] == "file"
    assert row["stored_name"] == stored
    assert os.path.exists(os.path.join(vesta_app.UPLOAD_DIR, stored))
    assert not os.path.exists(os.path.join(vesta_app.UPLOAD_DIR, stored + ".part"))

    vesta_app.fetch_canvas_material(mid)          # the second click
    assert len(calls) == 1


def test_a_failed_fetch_leaves_the_link_as_it_was(conn, class_id, monkeypatch):
    import app as vesta_app

    connect(conn, class_id)
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, semester_id, class_id, title, kind, url, filename,"
                 " import_key, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (mid, db.active_semester_id(conn), class_id, "Big.mp4", "link",
                  "https://canvas.sfu.ca/files/9/download", "Big.mp4", "canvas:file:9",
                  "2026-09-01"))
    conn.commit()

    def too_big(self, url, dest, max_bytes=None):
        raise canvas.CanvasError("That file is 113 MB, over Vesta's 100 MB limit.")

    monkeypatch.setattr(canvas.Client, "download", too_big)
    with pytest.raises(canvas.CanvasError):
        vesta_app.fetch_canvas_material(mid)
    row = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    assert (row["kind"], row["stored_name"]) == ("link", None)


def test_a_replaced_file_swaps_in_the_new_copy_and_keeps_the_old_for_undo(conn, class_id, monkeypatch):
    """The old copy stays on disk: the undo history owns it until the entry ages out."""
    import app as vesta_app

    connect(conn, class_id)
    old = "old-%s.txt" % uuid.uuid4()
    with open(os.path.join(vesta_app.UPLOAD_DIR, old), "wb") as fh:
        fh.write(b"v1")
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, semester_id, class_id, title, kind, url, filename,"
                 " stored_name, import_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (mid, db.active_semester_id(conn), class_id, "Notes.txt", "file",
                  "https://canvas.sfu.ca/files/9/download", "Notes.txt", old,
                  "canvas:file:9", "2026-09-01"))
    conn.commit()

    def v2(self, url, dest, max_bytes=None):
        with open(dest, "wb") as fh:
            fh.write(b"v2")
        return 2

    monkeypatch.setattr(canvas.Client, "download", v2)
    stored = vesta_app.fetch_canvas_material(mid, replace=True)
    assert stored != old
    assert os.path.exists(os.path.join(vesta_app.UPLOAD_DIR, old))
    with open(os.path.join(vesta_app.UPLOAD_DIR, stored), "rb") as fh:
        assert fh.read() == b"v2"


def test_a_class_made_from_a_course_is_named_and_coloured(conn):
    """Not "SD381 OL01" on a colourless card: the only one on the dashboard without one."""
    cid = sync._class_from_course(conn, {"course_code": "SD381 OL01",
                                         "name": "SD381 OL01 Building Sustainable Communities"})
    row = conn.execute("SELECT code, name, color FROM classes WHERE id=?", (cid,)).fetchone()
    assert (row["code"], row["name"]) == ("SD 381", "Building Sustainable Communities")
    assert row["color"] in sync.CLASS_PALETTE
    conn.execute("DELETE FROM classes WHERE id=?", (cid,))
    conn.commit()


def test_the_banner_tells_a_new_grade_from_one_that_disagrees(conn, class_id):
    add_item(conn, class_id, title="Quiz 01", import_key="canvas:100",
             due_date="2026-09-16", due_time="11:20", weight=5.0, score=85.0)
    add_item(conn, class_id, title="Quiz 02", import_key="canvas:101",
             due_date="2026-09-16", due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[
        planned(score=90.0),
        planned(canvasId=101, importKey="canvas:101", title="Quiz 02", score=70.0)])
    assert review(conn)["headlines"] == ["1 new grade", "1 grade that differs from yours"]

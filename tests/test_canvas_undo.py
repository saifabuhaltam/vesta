"""Undoing a Canvas apply, and reading deadlines in the Canvas account's time zone.

Undo's promise is narrow and has to be exact: take back what an apply did, offer it
again, and never destroy anything he has done since. Most of these tests are the
second half of that sentence.
"""
import os
import uuid

import pytest

import canvas
import canvas_sync as sync
import db
from test_canvas_review import (COURSE, conn, class_id, planned, cfile, connect,   # noqa: F401
                                add_item, review, units, item_row)


def apply_all(conn, reject_kinds=()):
    """Accept everything in the review, or refuse the kinds named. Returns the undo id."""
    state = sync.load_state(conn)
    rev = sync.build_review(conn, state)
    acc, rej = [], []
    for u in (u for c in rev["classes"] for g in c["groups"] for u in g["changes"]):
        (rej if u["kind"] in reject_kinds else acc).append(u["id"])
    report, to_fetch = sync.apply_selection(conn, state, acc, rej)
    conn.commit()
    sync.save_state(conn, state)
    entry = report.pop("_undo", None)
    if entry:
        sync.record_undo(conn, entry)
    return entry["id"] if entry else None, report


def undo(conn, undo_id):
    state = sync.load_state(conn)
    entry = next(e for e in sync.load_undo(conn) if e["id"] == undo_id)
    report, remove = sync.undo_apply(conn, state, entry)
    conn.commit()
    sync.save_state(conn, state)
    sync.save_undo(conn, [e for e in sync.load_undo(conn) if e["id"] != undo_id])
    for name in remove:
        try:
            os.remove(os.path.join(db.UPLOAD_DIR, name))
        except OSError:
            pass
    return report


def count(conn, table, class_id):
    return conn.execute("SELECT COUNT(*) AS n FROM %s WHERE class_id=?" % table,
                        (class_id,)).fetchone()["n"]


@pytest.fixture(autouse=True)
def fresh_history(conn):
    sync.save_undo(conn, [])
    conn.commit()
    yield


# ---------------------------------------------------------------------------
# the guard on the dependents lists
# ---------------------------------------------------------------------------

def test_every_table_that_hangs_off_an_assignment_or_file_is_known():
    """If a table referencing items or materials is added, undo must learn about it,
    or undoing could delete an assignment with his work cascading away underneath."""
    db.init_db()
    c = db.get_db()
    live = {"items": set(), "materials": set()}
    for (t,) in [(r["name"],) for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
        for fk in c.execute("PRAGMA foreign_key_list(%s)" % t).fetchall():
            if fk["table"] in live:
                live[fk["table"]].add((t, fk["from"]))
    c.close()
    assert live["items"] == {(t, col) for t, col, _ in sync.ITEM_DEPENDENTS}
    assert live["materials"] == {(t, col) for t, col, _ in sync.MATERIAL_DEPENDENTS}


# ---------------------------------------------------------------------------
# taking an apply back
# ---------------------------------------------------------------------------

def test_undo_takes_back_everything_an_accept_all_added(conn, class_id):
    connect(conn, class_id,
            items=[planned(categoryCanvasId=1, weight=None),
                   planned(canvasId=101, importKey="canvas:101", title="Quiz 02",
                           categoryCanvasId=1, weight=None)],
            categories=[{"canvasId": 1, "name": "Quizzes", "weight": 10.0, "dropLowest": 0}],
            files=[cfile(), cfile(id=501, display_name="Lecture 2.pdf")])
    before = review(conn)["count"]
    uid, _ = apply_all(conn)
    assert count(conn, "items", class_id) == 2 and count(conn, "materials", class_id) == 2

    report = undo(conn, uid)
    assert report["undone"]["items"] == 2
    assert report["undone"]["files"] == 2
    assert report["undone"]["categories"] == 1
    assert report["kept"] == []
    for table in ("items", "materials", "grade_categories"):
        assert count(conn, table, class_id) == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM file_folders WHERE class_id=? AND kind='custom'",
                        (class_id,)).fetchone()["n"] == 0
    # and all of it is offered again, not hidden as if he had deleted it
    after = review(conn)
    assert after["count"] == before
    assert after["classes"][0]["hidden"] == 0


def test_an_assignment_he_worked_on_since_is_kept(conn, class_id):
    connect(conn, class_id, items=[planned(),
                                   planned(canvasId=101, importKey="canvas:101", title="Quiz 02")])
    uid, _ = apply_all(conn)
    conn.execute("UPDATE items SET status='done' WHERE import_key='canvas:100'")
    conn.commit()
    report = undo(conn, uid)
    assert report["undone"]["items"] == 1
    assert [k["title"] for k in report["kept"]] == ["Quiz 01"]
    assert count(conn, "items", class_id) == 1


def test_an_assignment_with_a_subtask_is_kept_rather_than_cascaded_away(conn, class_id):
    connect(conn, class_id, items=[planned()])
    uid, _ = apply_all(conn)
    iid = conn.execute("SELECT id FROM items WHERE class_id=?", (class_id,)).fetchone()["id"]
    conn.execute("INSERT INTO subtasks (id, item_id, title, done) VALUES (?,?,?,0)",
                 (str(uuid.uuid4()), iid, "Outline"))
    conn.commit()
    report = undo(conn, uid)
    assert report["kept"] == [{"title": "Quiz 01", "why": "it has subtasks"}]
    assert item_row(conn, iid) is not None
    assert conn.execute("SELECT COUNT(*) AS n FROM subtasks WHERE item_id=?",
                        (iid,)).fetchone()["n"] == 1


def test_a_file_he_moved_or_attached_is_kept(conn, class_id):
    connect(conn, class_id, items=[planned()],
            files=[cfile(), cfile(id=501, display_name="Lecture 2.pdf")])
    uid, _ = apply_all(conn)
    mats = {r["title"]: r["id"] for r in conn.execute(
        "SELECT id, title FROM materials WHERE class_id=?", (class_id,))}
    conn.execute("UPDATE materials SET title='My notes on lecture 1' WHERE id=?",
                 (mats["Lecture 1.pdf"],))
    iid = conn.execute("SELECT id FROM items WHERE class_id=?", (class_id,)).fetchone()["id"]
    conn.execute("INSERT INTO item_files (id, item_id, material_id, created_at) VALUES (?,?,?,?)",
                 (str(uuid.uuid4()), iid, mats["Lecture 2.pdf"], "2026-09-22"))
    conn.commit()
    report = undo(conn, uid)
    reasons = {k["title"]: k["why"] for k in report["kept"]}
    assert reasons["My notes on lecture 1"] == "you moved or renamed it"
    assert reasons["Lecture 2.pdf"] == "it is attached to an assignment"
    # and the assignment the file hangs off stays too, rather than cascading the link away
    assert reasons["Quiz 01"] == "files are attached to it"
    assert count(conn, "materials", class_id) == 2


def test_undoing_a_moved_deadline_puts_the_old_date_back(conn, class_id):
    iid = add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-15",
                   due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[planned(dueDate="2026-09-16", weight=5.0, score=None)])
    uid, _ = apply_all(conn)
    assert item_row(conn, iid)["due_date"] == "2026-09-16"
    report = undo(conn, uid)
    assert report["undone"]["restored"] == 1
    assert item_row(conn, iid)["due_date"] == "2026-09-15"
    assert units(review(conn), "moved")                  # offered again


def test_a_date_he_changed_again_after_accepting_is_not_undone(conn, class_id):
    iid = add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-15",
                   due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[planned(dueDate="2026-09-16", weight=5.0, score=None)])
    uid, _ = apply_all(conn)
    conn.execute("UPDATE items SET due_date='2026-09-20' WHERE id=?", (iid,))
    conn.commit()
    report = undo(conn, uid)
    assert item_row(conn, iid)["due_date"] == "2026-09-20"
    assert report["kept"] == [{"title": "Quiz 01", "why": "you changed it again since"}]


def test_undoing_keep_mine_offers_the_change_again(conn, class_id):
    add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-16",
             due_time="11:20", weight=5.0, score=80.0)
    connect(conn, class_id, items=[planned(weight=5.0, score=95.0)])
    uid, _ = apply_all(conn, reject_kinds=("grade",))
    assert units(review(conn), "grade") == []
    undo(conn, uid)
    assert len(units(review(conn), "grade")) == 1


def test_undoing_a_skip_offers_the_assignment_again(conn, class_id):
    connect(conn, class_id, items=[planned(title="Reading 1")])
    uid, _ = apply_all(conn, reject_kinds=("newItem",))
    assert review(conn)["classes"][0]["hidden"] == 1
    undo(conn, uid)
    rev = review(conn)
    assert rev["classes"][0]["hidden"] == 0
    assert units(rev, "newItem")[0]["title"] == "Reading 1"


def test_undoing_a_replaced_file_brings_back_the_earlier_version(conn, class_id):
    old = "v1-%s.pdf" % uuid.uuid4()
    new = "v2-%s.pdf" % uuid.uuid4()
    for name in (old, new):
        with open(os.path.join(db.UPLOAD_DIR, name), "wb") as fh:
            fh.write(name.encode())
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, semester_id, class_id, title, kind, filename,"
                 " stored_name, size, import_key, url, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (mid, db.active_semester_id(conn), class_id, "Lecture 1.pdf", "file",
                  "Lecture 1.pdf", old, 1024, "canvas:file:500",
                  "https://canvas.sfu.ca/files/500/download", "2026-09-01"))
    conn.commit()
    connect(conn, class_id, files=[cfile(size=2048)])
    uid, _ = apply_all(conn)
    # the fetch that follows swaps the new bytes in
    conn.execute("UPDATE materials SET stored_name=? WHERE id=?", (new, mid))
    conn.commit()
    undo(conn, uid)
    row = conn.execute("SELECT stored_name, size FROM materials WHERE id=?", (mid,)).fetchone()
    assert (row["stored_name"], row["size"]) == (old, 1024)
    assert os.path.exists(os.path.join(db.UPLOAD_DIR, old))
    assert not os.path.exists(os.path.join(db.UPLOAD_DIR, new))


def test_the_history_keeps_ten_and_lets_an_old_replaced_copy_go(conn, class_id):
    old = "aged-%s.pdf" % uuid.uuid4()
    with open(os.path.join(db.UPLOAD_DIR, old), "wb") as fh:
        fh.write(b"x")
    sync.record_undo(conn, {"id": "oldest", "at": sync._now(), "summary": "", "journal": [
        {"op": "mat~", "id": "gone", "class": class_id, "before": {"stored_name": old}}]})
    for i in range(sync.UNDO_KEEP):
        sync.record_undo(conn, {"id": "e%d" % i, "at": sync._now(), "summary": "", "journal": []})
    conn.commit()
    ids = [e["id"] for e in sync.load_undo(conn)]
    assert len(ids) == sync.UNDO_KEEP and "oldest" not in ids
    assert not os.path.exists(os.path.join(db.UPLOAD_DIR, old))


def test_a_download_that_lands_after_an_undo_is_thrown_away(conn, class_id, monkeypatch):
    """A big file can still be downloading when he presses Undo. It must not come back."""
    import app as vesta_app

    connect(conn, class_id)
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, semester_id, class_id, title, kind, url, filename,"
                 " import_key, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (mid, db.active_semester_id(conn), class_id, "Deck.pdf", "link",
                  "https://canvas.sfu.ca/files/9/download", "Deck.pdf", "canvas:file:9",
                  "2026-09-01"))
    conn.commit()
    written = []

    def slow_download(self, url, dest, max_bytes=None):
        with open(dest, "wb") as fh:
            fh.write(b"%PDF")
        written.append(dest)
        c2 = db.get_db()                    # the undo lands mid-download
        c2.execute("DELETE FROM materials WHERE id=?", (mid,))
        c2.commit()
        c2.close()
        return 4

    monkeypatch.setattr(canvas.Client, "download", slow_download)
    assert vesta_app.fetch_canvas_material(mid) is None
    final = written[0][:-len(".part")]
    assert not os.path.exists(final)


# ---------------------------------------------------------------------------
# through the routes
# ---------------------------------------------------------------------------

from test_auth import FakeSupabase, supa, client, sign_in     # noqa: E402,F401


@pytest.fixture
def signed_in(client, supa):
    sign_in(client, supa)
    return client


def test_apply_hands_back_an_undo_and_the_review_lists_it(signed_in, conn, class_id):
    connect(conn, class_id, items=[planned()])
    body = signed_in.get("/api/canvas/review").get_json()
    ids = [u["id"] for g in body["groups"] for u in g["changes"]]
    out = signed_in.post("/api/canvas/apply", json={"accept": ids, "reject": []}).get_json()
    assert out["undoId"]
    hist = signed_in.get("/api/canvas/review").get_json()["history"]
    assert hist[0]["id"] == out["undoId"]
    assert hist[0]["summary"] == "Added 1 assignment"
    assert "journal" not in repr(hist)

    done = signed_in.post("/api/canvas/undo", json={"id": out["undoId"]}).get_json()
    assert done["message"] == "Undone: removed 1 assignment."
    assert done["review"]["count"] == 1
    assert signed_in.get("/api/canvas/review").get_json()["history"] == []


def test_undoing_something_already_undone_says_so(signed_in, conn, class_id):
    r = signed_in.post("/api/canvas/undo", json={"id": "not-a-real-one"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# the Canvas account's time zone
# ---------------------------------------------------------------------------

def test_a_deadline_reads_the_way_canvas_shows_it_in_the_accounts_zone():
    """Discussion 08: 23:59 on Nov 15 in Canvas, stored as 07:59 UTC on Nov 16. The time
    zone database has Vancouver on permanent UTC-7 from November, which read it as
    00:59 on Nov 16. His Canvas account is set to Los Angeles, which is what Canvas
    shows him, and what Vesta must match."""
    import ics
    la = ics.zone("America/Los_Angeles")
    assert canvas.due_local("2026-11-16T07:59:59Z", la) == ("2026-11-15", "23:59")
    assert canvas.due_local("2026-11-24T19:20:00Z", la) == ("2026-11-24", "11:20")
    assert canvas.due_local("2026-10-06T18:20:00Z", la) == ("2026-10-06", "11:20")


def test_a_check_converts_in_the_zone_his_account_uses(conn, class_id, monkeypatch):
    connect(conn, class_id)

    class FakeClient:
        def __init__(self, host, token):
            pass

        def profile(self):
            return {"time_zone": "America/Los_Angeles"}

        def course(self, cid):
            return {"id": cid, "apply_assignment_group_weights": True}

        def assignment_groups(self, cid):
            return [{"id": 1, "name": "Discussions", "group_weight": 10, "assignments": [
                {"id": 8, "name": "Discussion 08", "points_possible": 3, "published": True,
                 "due_at": "2026-11-16T07:59:59Z"},
                {"id": 9, "name": "Discussion 09", "points_possible": 3, "published": True,
                 "due_at": "2026-11-23T07:59:59Z"}]}]

        def course_files(self, cid, known=None):
            return []

    monkeypatch.setattr(canvas, "Client", FakeClient)
    sync.check_account(None)
    snap = sync.load_snapshot(conn, COURSE)
    assert snap["timeZone"] == "America/Los_Angeles"
    assert [(i["dueDate"], i["dueTime"]) for i in snap["plan"]["items"]] == \
        [("2026-11-15", "23:59"), ("2026-11-22", "23:59")]


def test_a_profile_canvas_will_not_give_falls_back_rather_than_failing(conn, class_id, monkeypatch):
    connect(conn, class_id)

    class NoProfile:
        def __init__(self, host, token):
            pass

        def profile(self):
            raise canvas.CanvasError("Canvas said no", 403)

        def course(self, cid):
            return {"id": cid}

        def assignment_groups(self, cid):
            return []

        def course_files(self, cid, known=None):
            return []

    monkeypatch.setattr(canvas, "Client", NoProfile)
    assert sync.check_account(None)["checked"] == 1
    assert sync.load_snapshot(conn, COURSE)["timeZone"] == ""


# ---------------------------------------------------------------------------
# filling a description he left empty
# ---------------------------------------------------------------------------

def test_an_empty_description_is_its_own_group_and_fills_in(conn, class_id):
    iid = add_item(conn, class_id, title="Discussion 08", import_key="canvas:100",
                   due_date="2026-09-16", due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[planned(title="Discussion 08", weight=5.0, score=None,
                                           notes="- Discuss two pros and two cons.")])
    rev = review(conn)
    assert [g["kind"] for g in rev["groups"]] == ["description"]
    assert rev["headlines"] == ["1 description to fill in"]
    uid, _ = apply_all(conn)
    assert item_row(conn, iid)["notes"] == "- Discuss two pros and two cons."
    assert review(conn)["count"] == 0
    undo(conn, uid)
    assert (item_row(conn, iid)["notes"] or "") == ""
    assert review(conn)["count"] == 1


def test_skipping_a_description_is_remembered(conn, class_id):
    add_item(conn, class_id, title="Discussion 08", import_key="canvas:100",
             due_date="2026-09-16", due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[planned(title="Discussion 08", weight=5.0, score=None,
                                           notes="Discuss two pros and two cons.")])
    apply_all(conn, reject_kinds=("description",))
    assert review(conn)["count"] == 0


def test_a_description_saved_squashed_is_offered_canvas_s_layout(conn, class_id):
    """Design Reflection as he found it: accepted before the formatting was kept."""
    iid = add_item(conn, class_id, title="Design Reflection", import_key="canvas:100",
                   due_date="2026-09-16", due_time="11:20", weight=5.0)
    conn.execute("UPDATE items SET notes=? WHERE id=?",
                 ("Outcomes The intent of this assignment. 1. What inspires you? 2. What problem?", iid))
    conn.commit()
    laid_out = "**Outcomes**\nThe intent of this assignment.\n\n1. What inspires you?\n2. What problem?"
    connect(conn, class_id, items=[planned(title="Design Reflection", weight=5.0, score=None,
                                           notes=laid_out)])
    rev = review(conn)
    assert [g["kind"] for g in rev["groups"]] == ["layout"]
    assert rev["headlines"] == ["1 description to lay out"]
    uid, _ = apply_all(conn)
    assert item_row(conn, iid)["notes"] == laid_out
    undo(conn, uid)
    assert item_row(conn, iid)["notes"].startswith("Outcomes The intent")


def test_a_description_he_changed_is_never_offered_a_new_layout(conn, class_id):
    iid = add_item(conn, class_id, title="Design Reflection", import_key="canvas:100",
                   due_date="2026-09-16", due_time="11:20", weight=5.0)
    conn.execute("UPDATE items SET notes=? WHERE id=?",
                 ("Outcomes The intent of this assignment. Start early, ask Prof about Q3.", iid))
    conn.commit()
    connect(conn, class_id, items=[planned(title="Design Reflection", weight=5.0, score=None,
                                           notes="**Outcomes**\nThe intent of this assignment.")])
    assert review(conn)["count"] == 0

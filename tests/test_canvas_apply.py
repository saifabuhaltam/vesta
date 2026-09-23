"""Applying a reviewed Canvas draft: only what was ticked, and nothing else.

These run against a real SQLite database rather than stubs, because the point of them
is what ends up in the rows: that a second sync updates instead of duplicating, that an
assignment he has been working on keeps its status and its title, and that a file
arrives as a link whose bytes have not been fetched yet.
"""
import uuid

import pytest

import canvas_sync as sync
import db


@pytest.fixture
def conn():
    db.init_db()
    c = db.get_db()
    yield c
    c.close()


@pytest.fixture
def class_id(conn):
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO classes (id, semester_id, code, name, created_at)"
        " VALUES (?,?,?,?,?)",
        (cid, db.active_semester_id(conn), "REM388", "Wildlife Conservation", "2026-09-01"))
    conn.commit()
    yield cid
    for table in ("materials", "items", "grade_categories", "file_folders"):
        conn.execute("DELETE FROM %s WHERE class_id=?" % table, (cid,))
    conn.execute("DELETE FROM classes WHERE id=?", (cid,))
    conn.commit()


def item_draft(**kw):
    base = {"importKey": "canvas:100", "title": "Quiz 01", "type": "quiz",
            "dueDate": "2026-09-15", "dueTime": "11:20", "notes": "Read chapter 1.",
            "weight": 10.0, "score": None, "categoryCanvasId": None,
            "change": "new", "existingId": None, "changes": [], "include": True}
    base.update(kw)
    return base


def file_draft(**kw):
    base = {"importKey": "canvas:file:500", "title": "Lecture 1.pdf",
            "filename": "Lecture 1.pdf", "size": 1024, "mimetype": "application/pdf",
            "url": "https://canvas.sfu.ca/files/500/download", "module": "Week 1",
            "folderName": "Week 1", "change": "new", "existingId": None, "include": True}
    base.update(kw)
    return base


def draft(items=None, files=None, categories=None):
    return {"items": items or [], "files": files or [], "categories": categories or []}


def rows(conn, table, class_id):
    return conn.execute("SELECT * FROM %s WHERE class_id=?" % table, (class_id,)).fetchall()


# ---------------------------------------------------------------------------
# assignments
# ---------------------------------------------------------------------------

def test_a_new_assignment_lands_with_its_canvas_key(conn, class_id):
    counts, _, keys = sync.apply_draft(conn, class_id, draft(items=[item_draft()]))
    conn.commit()
    assert counts["items"] == 1
    row = rows(conn, "items", class_id)[0]
    assert row["title"] == "Quiz 01"
    assert row["type"] == "quiz"
    assert (row["due_date"], row["due_time"]) == ("2026-09-15", "11:20")
    assert row["import_key"] == "canvas:100"
    assert row["status"] == "todo"
    assert keys == ["canvas:100"]


def test_an_unticked_assignment_is_not_written(conn, class_id):
    sync.apply_draft(conn, class_id, draft(items=[item_draft(include=False)]))
    conn.commit()
    assert rows(conn, "items", class_id) == []


def test_a_dismissed_assignment_is_not_written_even_if_ticked(conn, class_id):
    """He deleted it. A tick arriving from a stale screen does not bring it back."""
    sync.apply_draft(conn, class_id, draft(items=[item_draft(change="dismissed",
                                                             include=True)]))
    conn.commit()
    assert rows(conn, "items", class_id) == []


def test_only_the_ticked_fields_of_an_existing_assignment_change(conn, class_id):
    """The heart of "Canvas wins unless I changed it by hand"."""
    iid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO items (id, semester_id, class_id, title, type, due_date, due_time,"
        " status, weight, score, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (iid, db.active_semester_id(conn), class_id, "My own title", "quiz",
         "2026-09-10", "09:00", "done", 25.0, 88.0, "2026-09-01"))
    conn.commit()

    sync.apply_draft(conn, class_id, draft(items=[item_draft(
        existingId=iid, change="changed", include=True, title="Quiz 01 (renamed)",
        changes=[
            {"field": "dueDate", "before": "2026-09-10", "after": "2026-09-15",
             "fill": False, "include": True},
            {"field": "score", "before": 88.0, "after": 91.0, "fill": False,
             "include": False},
        ])]))
    conn.commit()

    row = rows(conn, "items", class_id)[0]
    assert row["due_date"] == "2026-09-15"      # ticked
    assert row["score"] == 88.0                 # not ticked, his number kept
    assert row["title"] == "My own title"       # a sync never renames what exists
    assert row["status"] == "done"              # nor un-finishes it
    assert row["weight"] == 25.0                # nor touches a weight he set
    assert row["import_key"] == "canvas:100"    # but it does claim the row


def test_an_unchanged_assignment_still_gains_its_key(conn, class_id):
    """Matched by title the first time; matched exactly every time after."""
    iid = str(uuid.uuid4())
    conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, status,"
                 " created_at) VALUES (?,?,?,?,?,?,?)",
                 (iid, db.active_semester_id(conn), class_id, "Quiz 01", "quiz",
                  "todo", "2026-09-01"))
    conn.commit()
    sync.apply_draft(conn, class_id, draft(items=[item_draft(
        existingId=iid, change="same", include=True, changes=[])]))
    conn.commit()
    assert rows(conn, "items", class_id)[0]["import_key"] == "canvas:100"


def test_a_change_with_no_word_either_way_follows_the_fill_rule(conn, class_id):
    """A screen that sends no `include` gets the default `compare` computed: a gap is
    filled, a disagreement is not."""
    iid = str(uuid.uuid4())
    conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, status,"
                 " score, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (iid, db.active_semester_id(conn), class_id, "Quiz 01", "quiz",
                  "todo", 70.0, "2026-09-01"))
    conn.commit()
    sync.apply_draft(conn, class_id, draft(items=[item_draft(
        existingId=iid, change="changed", include=True, changes=[
            {"field": "dueDate", "before": None, "after": "2026-09-15", "fill": True},
            {"field": "score", "before": 70.0, "after": 91.0, "fill": False},
        ])]))
    conn.commit()
    row = rows(conn, "items", class_id)[0]
    assert row["due_date"] == "2026-09-15"
    assert row["score"] == 70.0


# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------

def test_a_category_is_created_and_takes_the_weight_off_the_item(conn, class_id):
    """An item in a category must not also carry a weight, or the grade counts twice."""
    counts, _, _ = sync.apply_draft(conn, class_id, draft(
        categories=[{"canvasId": 10, "name": "Quizzes", "weight": 10.0, "dropLowest": 1}],
        items=[item_draft(categoryCanvasId=10, weight=None)]))
    conn.commit()
    assert counts["categories"] == 1
    cat = rows(conn, "grade_categories", class_id)[0]
    assert (cat["name"], cat["weight"], cat["drop_lowest"]) == ("Quizzes", 10.0, 1)
    item = rows(conn, "items", class_id)[0]
    assert item["category_id"] == cat["id"]
    assert item["weight"] is None


def test_a_category_he_already_has_is_reused_and_keeps_its_weight(conn, class_id):
    """Nothing he has is changed without being reviewed, and that includes a weight."""
    conn.execute("INSERT INTO grade_categories (id, class_id, name, weight, drop_lowest,"
                 " sort_order, created_at) VALUES (?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()), class_id, "quizzes", 5.0, 0, 0, "2026-09-01"))
    conn.commit()
    sync.apply_draft(conn, class_id, draft(
        categories=[{"canvasId": 10, "name": "Quizzes", "weight": 10.0, "dropLowest": 1}]))
    conn.commit()
    cats = rows(conn, "grade_categories", class_id)
    assert len(cats) == 1
    assert cats[0]["weight"] == 5.0


def test_an_accepted_category_change_is_applied(conn, class_id):
    conn.execute("INSERT INTO grade_categories (id, class_id, name, weight, drop_lowest,"
                 " sort_order, created_at) VALUES (?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()), class_id, "quizzes", 5.0, 0, 0, "2026-09-01"))
    conn.commit()
    sync.apply_draft(conn, class_id, draft(categories=[
        {"canvasId": 10, "name": "Quizzes", "weight": 10.0, "dropLowest": 1,
         "applyWeight": True}]))
    conn.commit()
    cat = rows(conn, "grade_categories", class_id)[0]
    assert (cat["weight"], cat["drop_lowest"]) == (10.0, 1)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def test_a_file_arrives_as_a_link_whose_bytes_are_not_here_yet(conn, class_id):
    counts, to_fetch, keys = sync.apply_draft(conn, class_id, draft(files=[file_draft()]))
    conn.commit()
    assert counts["files"] == 1
    row = rows(conn, "materials", class_id)[0]
    assert row["kind"] == "link"
    assert row["stored_name"] is None          # the marker for "not fetched yet"
    assert row["url"].endswith("/files/500/download")
    assert row["import_key"] == "canvas:file:500"
    assert row["size"] == 1024
    assert keys == ["canvas:file:500"]
    assert to_fetch[0]["materialId"] == row["id"]


def test_a_module_becomes_a_folder_and_the_next_file_reuses_it(conn, class_id):
    sync.apply_draft(conn, class_id, draft(files=[
        file_draft(),
        file_draft(importKey="canvas:file:501", filename="Lecture 2.pdf",
                   title="Lecture 2.pdf"),
    ]))
    conn.commit()
    folders = rows(conn, "file_folders", class_id)
    assert [f["name"] for f in folders] == ["Week 1"]
    assert folders[0]["kind"] == "custom"       # renameable, like one he made
    assert {r["folder_id"] for r in rows(conn, "materials", class_id)} == {folders[0]["id"]}


def test_a_folder_he_already_has_is_not_made_twice(conn, class_id):
    fid = str(uuid.uuid4())
    conn.execute("INSERT INTO file_folders (id, class_id, parent_id, name, kind,"
                 " sort_order, created_at) VALUES (?,?,?,?,?,?,?)",
                 (fid, class_id, None, "week 1", "custom", 0, "2026-09-01"))
    conn.commit()
    sync.apply_draft(conn, class_id, draft(files=[file_draft()]))
    conn.commit()
    assert len(rows(conn, "file_folders", class_id)) == 1
    assert rows(conn, "materials", class_id)[0]["folder_id"] == fid


def test_a_filename_still_gets_its_category_guess(conn, class_id):
    """The same guess an upload gets, so a Canvas rubric is still a rubric."""
    sync.apply_draft(conn, class_id, draft(files=[file_draft(filename="Essay Rubric.docx")]),
                     guess_category=lambda name: "rubrics" if "rubric" in name.lower() else "other")
    conn.commit()
    assert rows(conn, "materials", class_id)[0]["category"] == "rubrics"


# ---------------------------------------------------------------------------
# what gets fetched now
# ---------------------------------------------------------------------------

def test_a_small_file_is_fetched_and_a_big_one_is_left_as_a_link(conn, class_id):
    _, to_fetch, _ = sync.apply_draft(conn, class_id, draft(files=[
        file_draft(importKey="canvas:file:1", filename="Syllabus.pdf", size=400 * 1024),
        file_draft(importKey="canvas:file:2", filename="Lecture deck.pptx",
                   size=40 * 1024 * 1024),
    ]))
    conn.commit()
    assert [f["filename"] for f in to_fetch] == ["Syllabus.pdf"]
    assert len(rows(conn, "materials", class_id)) == 2      # both rows exist either way


@pytest.mark.parametrize("filename,mimetype", [
    ("Lecture recording.mp4", "video/mp4"),
    ("Seminar.mov", None),
    ("clip.webm", None),
])
def test_video_is_never_fetched_however_small(conn, class_id, filename, mimetype):
    """283 MB of his library is mp4, and none of it yields text to search."""
    _, to_fetch, _ = sync.apply_draft(conn, class_id, draft(files=[
        file_draft(filename=filename, mimetype=mimetype, size=1024)]))
    conn.commit()
    assert to_fetch == []


def test_a_file_whose_size_canvas_did_not_report_is_fetched(conn, class_id):
    """Guessing the other way would leave a syllabus unsearchable."""
    _, to_fetch, _ = sync.apply_draft(conn, class_id, draft(files=[file_draft(size=None)]))
    conn.commit()
    assert len(to_fetch) == 1


# ---------------------------------------------------------------------------
# the stored state
# ---------------------------------------------------------------------------

def test_the_token_never_comes_back_out_to_the_browser(conn):
    state = dict(sync.DEFAULT_STATE, token="secret-token", host="canvas.sfu.ca")
    public = sync.public_state(state)
    assert "secret-token" not in repr(public)
    assert public["connected"] is True
    assert public["host"] == "canvas.sfu.ca"


def test_state_survives_a_round_trip_through_the_database(conn):
    state = dict(sync.DEFAULT_STATE, token="t", host="canvas.sfu.ca")
    sync.remember(state, 55, "class-1", ["canvas:1", "canvas:2"])
    sync.save_state(conn, state)
    back = sync.load_state(conn)
    assert back["token"] == "t"
    assert sync.seen_keys(back, 55) == {"canvas:1", "canvas:2"}
    assert sync.course_state(back, 55)["classId"] == "class-1"
    conn.execute("DELETE FROM app_settings WHERE key=?", (sync.SETTINGS_KEY,))
    conn.commit()


def test_an_unconnected_account_reads_as_empty_rather_than_failing(conn):
    assert sync.load_state(conn)["token"] == ""
    assert sync.public_state(sync.load_state(conn))["connected"] is False


def test_forgetting_a_key_offers_it_again(conn):
    """What "show the ones I deleted" does."""
    state = dict(sync.DEFAULT_STATE)
    sync.remember(state, 55, "class-1", ["canvas:1", "canvas:2"])
    sync.forget(state, 55, ["canvas:1"])
    assert sync.seen_keys(state, 55) == {"canvas:2"}


def test_a_deleted_assignment_is_not_offered_again(conn):
    """The end-to-end of it: synced once, deleted by him, absent from the next review."""
    plan = {"items": [{"importKey": "canvas:100", "title": "Reading 1",
                       "type": "reading", "dueDate": None, "dueTime": None,
                       "weight": None, "score": None, "categoryCanvasId": None}],
            "categories": [], "weightsFromCanvas": True}
    fresh = sync.compare([], [], plan, [])
    assert fresh["summary"]["newItems"] == 1

    after_delete = sync.compare([], [], plan, [], seen={"canvas:100"})
    assert after_delete["summary"]["newItems"] == 0
    assert after_delete["summary"]["dismissed"] == 1
    assert after_delete["dismissed"]["items"][0]["title"] == "Reading 1"

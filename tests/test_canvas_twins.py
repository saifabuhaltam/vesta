"""The same assignment under two names, and grade categories that count it twice.

SD 381 on vesta.study, 2026-09-22: "Reading quiz (Week 1)" from the syllabus import sat
done beside Canvas's "Week 1 Readings Quiz", overdue, with the two in different grade
categories. These tests are that case and the rules around it: his row survives and is
linked to Canvas, Canvas's copy goes, an emptied Canvas category goes with it, and
nothing is merged that is not plainly the same assignment.
"""
import uuid

import pytest

import canvas_sync as sync
import db
from test_canvas_review import (COURSE, conn, class_id, planned, connect,   # noqa: F401
                                add_item, review, units, item_row)
from test_canvas_undo import apply_all, undo, fresh_history                  # noqa: F401


# ---------------------------------------------------------------------------
# telling one assignment from two
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mine,theirs", [
    # straight from his SD 381
    ("Reading quiz (Week 1)", "Week 1 Readings Quiz"),
    ("Second discussion: Writing good paragraphs",
     "MODULE 1, Week 1, Part 2 Activity/ Second Discussion activity- writing good paragraphs"),
    ("Short discussion (Week 2)", "MODULE 1, Week 2, short discussion on adulting"),
    ("First discussion: Introduce yourself", "MODULE 1, Week 1, Introduce Yourself"),
    # and the other shapes a syllabus uses
    ("Quiz 3", "Quiz 03 - Evolution by Natural Selection"),
    ("Discussion 6", "Discussion 06 - Industrial Challenges - Issues 1 to 3"),
    ("Midterm exam 1", "In-lecture midterm exam 1"),
])
def test_the_same_assignment_under_two_names_is_recognised(mine, theirs):
    assert sync.looks_same({"title": mine}, {"title": theirs}) > 0


@pytest.mark.parametrize("mine,theirs", [
    ("Quiz 01 - Course Outline", "Quiz 02 - The Tree of Life"),
    ("Discussion 3", "Discussion 06 - Issues 1 to 3"),
    ("Reading quiz (Week 1)", "Reading quiz (Week 2)"),
    ("Assignment 1", "Assignment 2 - Seek"),
    ("Midterm", "Final Exam"),
    ("Quiz", "Quiz 02"),
    ("Week 3 reading", "Week 4 reading"),
    ("Discussion 06 - Industrial Challenges", "Discussion 07 - Industrial Challenges"),
    # one syllabus row for two separate Canvas quizzes is not one assignment
    ("Two short video quizzes (Week 2)", "MODULE 1, WEEK 2, Part 2 Activity- Video and Embedded quiz"),
])
def test_two_different_assignments_are_not_confused(mine, theirs):
    assert sync.looks_same({"title": mine}, {"title": theirs}) == 0


def test_due_dates_far_apart_rule_a_pair_out():
    a = {"title": "Reading quiz (Week 1)", "due_date": "2026-09-13"}
    assert sync.looks_same(a, {"title": "Week 1 Readings Quiz", "due_date": "2026-09-13"}) > 0
    assert sync.looks_same(a, {"title": "Week 1 Readings Quiz", "due_date": "2026-09-14"}) > 0
    assert sync.looks_same(a, {"title": "Week 1 Readings Quiz", "due_date": "2026-09-20"}) == 0


def test_a_quiz_is_not_a_discussion():
    assert sync.looks_same({"title": "Week 2 quiz", "type": "quiz"},
                           {"title": "Week 2 quiz", "type": "discussion"}) == 0


# ---------------------------------------------------------------------------
# his SD 381, as found
# ---------------------------------------------------------------------------

def sd381(conn, class_id):
    """His done syllabus row in his category, and Canvas's copy alone in Canvas's."""
    sem = db.active_semester_id(conn)
    mine_cat, canvas_cat = str(uuid.uuid4()), str(uuid.uuid4())
    for cid, name, weight in ((mine_cat, "Low-stakes weekly assignments", 35.0),
                              (canvas_cat, "Low-stakes weekly assignments module 1", 8.0)):
        conn.execute("INSERT INTO grade_categories (id, class_id, name, weight, drop_lowest,"
                     " sort_order, created_at) VALUES (?,?,?,?,?,?,?)",
                     (cid, class_id, name, weight, 0, 0, "2026-09-01"))
    mine = str(uuid.uuid4())
    conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, due_date, status,"
                 " score, category_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (mine, sem, class_id, "Reading quiz (Week 1)", "quiz", "2026-09-13", "done",
                  60.0, mine_cat, "2026-09-01"))
    copy = str(uuid.uuid4())
    conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, due_date, due_time,"
                 " status, score, category_id, import_key, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (copy, sem, class_id, "Week 1 Readings Quiz", "quiz", "2026-09-13", "23:59",
                  "todo", 60.0, canvas_cat, "canvas:100", "2026-09-20"))
    conn.commit()
    connect(conn, class_id,
            items=[planned(title="Week 1 Readings Quiz", type="quiz", dueDate="2026-09-13",
                           dueTime="23:59", score=60.0, weight=None, categoryCanvasId=1)],
            categories=[{"canvasId": 1, "name": "Low-stakes weekly assignments module 1",
                         "weight": 8.0, "dropLowest": 0}])
    return mine, copy, mine_cat, canvas_cat


def test_his_sd381_shows_a_possible_duplicate_first(conn, class_id):
    sd381(conn, class_id)
    rev = review(conn)
    assert rev["groups"][0]["kind"] == "twin"
    assert rev["headlines"][0] == "1 possible duplicate"
    twin = units(rev, "twin")[0]
    assert twin["mine"]["title"] == "Reading quiz (Week 1)"
    assert twin["mine"]["done"] is True
    assert twin["alreadyHere"] is True


def test_same_assignment_keeps_his_row_and_removes_canvas_s_copy(conn, class_id):
    mine, copy, mine_cat, canvas_cat = sd381(conn, class_id)
    uid, report = apply_all(conn)
    assert report["applied"]["merged"] == 1
    row = item_row(conn, mine)
    assert row["import_key"] == "canvas:100"            # linked: syncs from now on
    assert (row["status"], row["score"], row["category_id"]) == ("done", 60.0, mine_cat)
    assert item_row(conn, copy) is None                 # Canvas's copy gone
    # and the Canvas category it leaves empty goes too, or its 8% would still count
    cats = {r["id"] for r in conn.execute("SELECT id FROM grade_categories WHERE class_id=?",
                                          (class_id,))}
    assert cats == {mine_cat}
    # Linked now, the next review only offers what Canvas can fill in on his row, and
    # calls it by his name: the due time his syllabus never had.
    left = units(review(conn))
    assert [(u["kind"], u["title"]) for u in left] == [("dateAdded", "Reading quiz (Week 1)")]


def test_undoing_a_merge_puts_everything_back(conn, class_id):
    mine, copy, mine_cat, canvas_cat = sd381(conn, class_id)
    uid, _ = apply_all(conn)
    undo(conn, uid)
    assert item_row(conn, mine)["import_key"] is None
    assert item_row(conn, copy)["title"] == "Week 1 Readings Quiz"
    assert conn.execute("SELECT 1 FROM grade_categories WHERE id=?", (canvas_cat,)).fetchone()
    assert units(review(conn), "twin")


def test_a_copy_he_worked_on_is_not_merged_away(conn, class_id):
    mine, copy, _, _ = sd381(conn, class_id)
    conn.execute("UPDATE items SET status='done' WHERE id=?", (copy,))
    conn.commit()
    _, report = apply_all(conn)
    assert report["applied"]["merged"] == 0
    assert report["notMerged"] == [{"title": "Week 1 Readings Quiz",
                                    "why": "you've worked on Canvas's copy"}]
    assert item_row(conn, copy) is not None
    assert item_row(conn, mine)["import_key"] is None


def test_different_is_remembered_and_leaves_both(conn, class_id):
    mine, copy, _, _ = sd381(conn, class_id)
    apply_all(conn, reject_kinds=("twin",))
    assert item_row(conn, copy) is not None
    assert units(review(conn), "twin") == []


# ---------------------------------------------------------------------------
# before a duplicate is made at all
# ---------------------------------------------------------------------------

def test_a_new_canvas_assignment_like_one_of_his_asks_instead_of_adding(conn, class_id):
    mine = add_item(conn, class_id, title="Short discussion (Week 2)", type="discussion",
                    due_date="2026-09-16")
    connect(conn, class_id, items=[planned(title="MODULE 1, Week 2, short discussion on adulting",
                                           type="discussion", dueDate="2026-09-16", weight=None,
                                           score=None, notes="Discuss adulting.")])
    rev = review(conn)
    assert [u["kind"] for u in units(rev)] == ["twin"]
    assert units(rev, "twin")[0]["alreadyHere"] is False
    apply_all(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM items WHERE class_id=?",
                        (class_id,)).fetchone()["n"] == 1
    assert item_row(conn, mine)["import_key"] == "canvas:100"
    # linked now, so the next review offers what Canvas can fill in on his row
    assert sorted(u["kind"] for u in units(review(conn))) == ["dateAdded", "description"]


def test_saying_different_to_a_new_one_offers_it_as_new(conn, class_id):
    add_item(conn, class_id, title="Short discussion (Week 2)", type="discussion",
             due_date="2026-09-16")
    connect(conn, class_id, items=[planned(title="MODULE 1, Week 2, short discussion on adulting",
                                           type="discussion", dueDate="2026-09-16", score=None)])
    apply_all(conn, reject_kinds=("twin",))
    assert [u["kind"] for u in units(review(conn))] == ["newItem"]


def test_an_exact_title_match_is_not_called_a_duplicate(conn, class_id):
    add_item(conn, class_id, title="Quiz 01", import_key=None, due_date="2026-09-16",
             due_time="11:20", weight=5.0)
    connect(conn, class_id, items=[planned(weight=5.0, score=None)])
    assert units(review(conn), "twin") == []


# ---------------------------------------------------------------------------
# grade categories that overlap
# ---------------------------------------------------------------------------

def overlap(conn, class_id):
    """His 60% and 40%, and Canvas's 70% and 30%, each with work in it: 200%."""
    sem = db.active_semester_id(conn)
    ids = {}
    for name, weight in (("Assignments", 60.0), ("Exams", 40.0), ("Canvas work", 70.0),
                         ("Canvas exams", 30.0)):
        ids[name] = str(uuid.uuid4())
        conn.execute("INSERT INTO grade_categories (id, class_id, name, weight, drop_lowest,"
                     " sort_order, created_at) VALUES (?,?,?,?,?,?,?)",
                     (ids[name], class_id, name, weight, 0, 0, "2026-09-01"))
    rows = {}
    for title, cat, key in (("Essay 1", "Assignments", "canvas:100"),
                            ("My own reading log", "Assignments", None),
                            ("Final", "Exams", "canvas:101"),
                            ("Canvas-only lab", "Canvas work", "canvas:102")):
        rows[title] = str(uuid.uuid4())
        conn.execute("INSERT INTO items (id, semester_id, class_id, title, type, status,"
                     " category_id, import_key, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (rows[title], sem, class_id, title, "assignment", "todo", ids[cat], key,
                      "2026-09-01"))
    conn.execute("UPDATE items SET due_date='2026-10-01', due_time='23:59' WHERE class_id=?",
                 (class_id,))
    conn.commit()
    connect(conn, class_id, categories=[
        {"canvasId": 1, "name": "Canvas work", "weight": 70.0, "dropLowest": 0},
        {"canvasId": 2, "name": "Canvas exams", "weight": 30.0, "dropLowest": 0}],
        items=[planned(canvasId=100, importKey="canvas:100", title="Essay 1", type="assignment",
                       dueDate="2026-10-01", dueTime="23:59", categoryCanvasId=1, score=None),
               planned(canvasId=101, importKey="canvas:101", title="Final", type="assignment",
                       dueDate="2026-10-01", dueTime="23:59", categoryCanvasId=2, score=None),
               planned(canvasId=102, importKey="canvas:102", title="Canvas-only lab",
                       type="assignment", dueDate="2026-10-01", dueTime="23:59",
                       categoryCanvasId=1, score=None)])
    return ids, rows


def test_overlapping_categories_are_shown_side_by_side(conn, class_id):
    overlap(conn, class_id)
    rev = review(conn)
    u = units(rev, "categories")[0]
    assert u["after"]["total"] == 200.0
    assert sorted(c["name"] for c in u["after"]["mine"]) == ["Assignments", "Exams"]
    assert sorted(c["name"] for c in u["after"]["canvas"]) == ["Canvas exams", "Canvas work"]
    assert rev["headlines"][0] == "REM388's grade weights add up to 200%"


def test_use_canvas_s_moves_his_linked_work_across_and_names_the_rest(conn, class_id):
    ids, rows = overlap(conn, class_id)
    uid, report = apply_all(conn)
    cat_of = lambda t: item_row(conn, rows[t])["category_id"]
    assert cat_of("Essay 1") == ids["Canvas work"]
    assert cat_of("Final") == ids["Canvas exams"]
    assert cat_of("My own reading log") is None
    assert report["leftOutOfCategories"] == ["My own reading log"]
    names = {r["name"] for r in conn.execute("SELECT name FROM grade_categories WHERE class_id=?",
                                             (class_id,))}
    assert names == {"Canvas work", "Canvas exams"}
    assert units(review(conn), "categories") == []

    undo(conn, uid)
    assert cat_of("Essay 1") == ids["Assignments"]
    assert cat_of("My own reading log") == ids["Assignments"]
    assert len(units(review(conn), "categories")) == 1


def test_keep_mine_removes_canvas_s_and_names_what_it_leaves_out(conn, class_id):
    ids, rows = overlap(conn, class_id)
    _, report = apply_all(conn, reject_kinds=("categories",))
    names = {r["name"] for r in conn.execute("SELECT name FROM grade_categories WHERE class_id=?",
                                             (class_id,))}
    assert names == {"Assignments", "Exams"}
    assert item_row(conn, rows["Essay 1"])["category_id"] == ids["Assignments"]
    assert item_row(conn, rows["Canvas-only lab"])["category_id"] is None
    assert report["leftOutOfCategories"] == ["Canvas-only lab"]


def test_categories_wait_until_duplicates_are_settled(conn, class_id):
    """Merging a duplicate can empty one of Canvas's categories, which then goes on its
    own; asking about categories first would ask about something about to change."""
    sd381(conn, class_id)
    conn.execute("UPDATE grade_categories SET weight=95 WHERE name='Low-stakes weekly assignments'")
    conn.commit()
    assert [g["kind"] for g in review(conn)["groups"]] == ["twin"]


def test_a_row_in_one_of_his_categories_is_never_offered_a_weight(conn, class_id):
    cat = str(uuid.uuid4())
    conn.execute("INSERT INTO grade_categories (id, class_id, name, weight, drop_lowest,"
                 " sort_order, created_at) VALUES (?,?,?,?,?,?,?)",
                 (cat, class_id, "Quizzes", 20.0, 0, 0, "2026-09-01"))
    conn.commit()
    add_item(conn, class_id, import_key="canvas:100", due_date="2026-09-16", due_time="11:20",
             category_id=cat)
    connect(conn, class_id, items=[planned(weight=5.0, score=None)])
    assert units(review(conn)) == []

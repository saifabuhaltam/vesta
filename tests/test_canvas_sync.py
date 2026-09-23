"""Canvas sync: nothing already in Vesta is overwritten, duplicated or deleted.

The rules under test are Saif's: Canvas fills what Vesta has not got, a disagreement is
shown rather than applied, an assignment he typed himself is recognised as the same
assignment, and a file already in the class is never silently replaced.
"""
import pytest

import canvas
import canvas_sync as sync


def plan(items, weighted=True, categories=None):
    return {"items": items, "categories": categories or [], "weightsFromCanvas": weighted}


def planned(**kw):
    """One assignment as `canvas.plan_course` would emit it."""
    base = {"canvasId": 100, "importKey": "canvas:100", "title": "Essay 1",
            "type": "assignment", "dueDate": "2026-10-01", "dueTime": "23:59",
            "notes": "", "url": None, "points": 20, "categoryCanvasId": None,
            "weight": 10.0, "score": None, "countsForGrade": True, "rubric": None}
    base.update(kw)
    return base


def row(**kw):
    """One `items` row as the database holds it."""
    base = {"id": "row-1", "title": "Essay 1", "type": "assignment", "due_date": None,
            "due_time": None, "weight": None, "score": None, "import_key": None,
            "category_id": None}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# assignments: matching
# ---------------------------------------------------------------------------

def test_an_empty_class_takes_everything():
    out = sync.compare_items([], plan([planned()]))
    assert out[0]["change"] == "new"
    assert out[0]["include"] is True
    assert out[0]["existingId"] is None


def test_a_second_sync_recognises_what_the_first_one_wrote():
    existing = [row(import_key="canvas:100", due_date="2026-10-01", due_time="23:59",
                    weight=10.0)]
    out = sync.compare_items(existing, plan([planned()]))
    assert out[0]["change"] == "same"
    assert out[0]["include"] is False
    assert out[0]["existingId"] == "row-1"


def test_an_assignment_he_typed_himself_is_not_duplicated():
    """The case that matters most on a first sync: a class whose assignments came from
    a syllabus import has no Canvas keys on any of them."""
    existing = [row(id="typed", title="Essay 1", import_key="essay 1|assignment")]
    out = sync.compare_items(existing, plan([planned()]))
    assert out[0]["change"] != "new"
    assert out[0]["existingId"] == "typed"


def test_matching_by_title_ignores_case_and_punctuation():
    existing = [row(id="typed", title="ESSAY #1!")]
    out = sync.compare_items(existing, plan([planned(title="essay 1")]))
    assert out[0]["existingId"] == "typed"


def test_one_row_cannot_match_two_canvas_assignments():
    """Two assignments genuinely called the same thing: the first takes the row, the
    second is new rather than fighting over it."""
    existing = [row(id="only-one", title="Quiz", due_date="2026-10-01",
                    due_time="23:59", weight=10.0)]
    out = sync.compare_items(existing, plan([
        planned(canvasId=1, importKey="canvas:1", title="Quiz"),
        planned(canvasId=2, importKey="canvas:2", title="Quiz"),
    ]))
    assert [i["existingId"] for i in out] == ["only-one", None]
    assert [i["change"] for i in out] == ["same", "new"]


def test_the_canvas_key_wins_over_a_title_that_also_matches():
    existing = [row(id="renamed", title="Old name", import_key="canvas:100"),
                row(id="same-title", title="Essay 1")]
    out = sync.compare_items(existing, plan([planned()]))
    assert out[0]["existingId"] == "renamed"


# ---------------------------------------------------------------------------
# assignments: what counts as a change
# ---------------------------------------------------------------------------

def test_an_empty_field_is_filled_without_asking():
    existing = [row(import_key="canvas:100")]
    out = sync.compare_items(existing, plan([planned()]))
    assert out[0]["change"] == "changed"
    assert out[0]["include"] is True
    assert all(c["fill"] for c in out[0]["changes"])


def test_a_disagreement_is_shown_with_its_tick_off():
    """'Canvas wins unless I change it by hand' is this: the tick is the hand."""
    existing = [row(import_key="canvas:100", due_date="2026-10-05", due_time="23:59",
                    weight=10.0)]
    out = sync.compare_items(existing, plan([planned()]))
    assert out[0]["change"] == "changed"
    assert out[0]["include"] is False
    change = next(c for c in out[0]["changes"] if c["field"] == "dueDate")
    assert (change["before"], change["after"]) == ("2026-10-05", "2026-10-01")
    assert change["fill"] is False


def test_a_score_he_typed_is_not_replaced_by_canvas():
    existing = [row(import_key="canvas:100", due_date="2026-10-01", due_time="23:59",
                    weight=10.0, score=88.0)]
    out = sync.compare_items(existing, plan([planned(score=91.0)]))
    assert out[0]["include"] is False
    change = next(c for c in out[0]["changes"] if c["field"] == "score")
    assert (change["before"], change["after"]) == (88.0, 91.0)


def test_a_score_vesta_never_had_is_filled():
    existing = [row(import_key="canvas:100", due_date="2026-10-01", due_time="23:59",
                    weight=10.0)]
    out = sync.compare_items(existing, plan([planned(score=91.0)]))
    assert out[0]["include"] is True


def test_a_due_date_canvas_does_not_have_is_not_a_change():
    """IAT201 carries no due dates at all. Absence is not an instruction to clear."""
    existing = [row(import_key="canvas:100", due_date="2026-10-05", due_time="23:59",
                    weight=10.0)]
    out = sync.compare_items(existing, plan([planned(dueDate=None, dueTime=None)]))
    assert out[0]["change"] == "same"
    assert out[0]["changes"] == []


def test_the_same_number_written_differently_is_not_a_change():
    existing = [row(import_key="canvas:100", due_date="2026-10-01", due_time="23:59",
                    weight=10)]
    out = sync.compare_items(existing, plan([planned(weight=10.0)]))
    assert out[0]["change"] == "same"


def test_a_points_course_never_offers_a_weight():
    """IAT201's weights are all 0% and PSYC300W's first assignment comes out at 62.5%.

    The first version offered them unticked. Run against his real courses, that left
    32 junk weight changes on every review forever, so they are not offered at all.
    """
    existing = [row(import_key="canvas:100", due_date="2026-10-01", due_time="23:59")]
    out = sync.compare_items(existing, plan([planned()], weighted=False))
    assert [c["field"] for c in out[0]["changes"]] == []
    assert out[0]["change"] == "same"


def test_an_assignment_vesta_has_and_canvas_does_not_is_left_alone():
    """Never offered for deletion. Canvas is one source among several in a class."""
    existing = [row(id="mine", title="Reading journal"), row(id="c", import_key="canvas:100")]
    out = sync.compare_items(existing, plan([planned()]))
    assert [i["existingId"] for i in out] == ["c"]
    assert "mine" not in [i["existingId"] for i in out]


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def cfile(**kw):
    base = {"id": 500, "display_name": "Lecture 1.pdf", "size": 1024,
            "content-type": "application/pdf",
            "url": "https://canvas.sfu.ca/files/500/download", "_module": "Week 1"}
    base.update(kw)
    return base


def material(**kw):
    base = {"id": "mat-1", "title": "Lecture 1.pdf", "filename": "Lecture 1.pdf",
            "size": 1024, "import_key": None}
    base.update(kw)
    return base


def test_a_file_the_class_does_not_have_is_taken():
    out = sync.compare_files([], [cfile()])
    assert out[0]["change"] == "new"
    assert out[0]["include"] is True
    assert out[0]["importKey"] == "canvas:file:500"


def test_a_file_already_synced_is_left_alone():
    out = sync.compare_files([material(import_key="canvas:file:500")], [cfile()])
    assert out[0]["change"] == "same"
    assert out[0]["include"] is False


def test_the_same_file_uploaded_by_hand_is_recognised_by_name_and_size():
    """He has been uploading these by hand all term. A sync must not double them."""
    out = sync.compare_files([material()], [cfile()])
    assert out[0]["change"] == "same"
    assert out[0]["existingId"] == "mat-1"


def test_a_file_whose_size_differs_is_a_conflict_not_a_replacement():
    """The copy already in Vesta may be the one he annotated, and it exists nowhere
    else. Replacing is never on the menu."""
    out = sync.compare_files([material(size=2048)], [cfile(size=999)])
    assert out[0]["change"] == "changed"
    assert out[0]["include"] is False
    assert "replace" not in out[0]["conflict"]["options"]
    assert out[0]["conflict"]["existingSize"] == 2048
    assert out[0]["conflict"]["canvasSize"] == 999


def test_a_file_stored_before_sizes_were_recorded_is_not_called_a_conflict():
    out = sync.compare_files([material(size=None)], [cfile()])
    assert out[0]["change"] == "same"


def test_a_file_keeps_the_module_it_came_from():
    out = sync.compare_files([], [cfile(_module="Week 3")])
    assert out[0]["module"] == "Week 3"
    assert out[0]["folderName"] == "Week 3"


def test_a_folder_mapping_overrides_the_module_name():
    out = sync.compare_files([], [cfile(_module="Week 3")], folder_for={"Week 3": "Lectures"})
    assert out[0]["folderName"] == "Lectures"


def test_one_material_cannot_match_two_canvas_files():
    out = sync.compare_files([material()], [cfile(id=500), cfile(id=501)])
    assert [f["change"] for f in out] == ["same", "new"]


# ---------------------------------------------------------------------------
# the draft as a whole
# ---------------------------------------------------------------------------

def test_the_summary_counts_what_the_screen_leads_with():
    draft = sync.compare(
        [row(id="c", import_key="canvas:100", due_date="2026-10-05", due_time="23:59",
             weight=10.0)],
        [material()],
        plan([planned(), planned(canvasId=101, importKey="canvas:101", title="Essay 2")]),
        [cfile(), cfile(id=501, display_name="Lecture 2.pdf")],
    )
    assert draft["summary"]["newItems"] == 1
    assert draft["summary"]["changedItems"] == 1
    assert draft["summary"]["newFiles"] == 1
    assert draft["summary"]["sameFiles"] == 1


def test_a_sync_that_would_change_nothing_says_so():
    existing = [row(import_key="canvas:100", due_date="2026-10-01", due_time="23:59",
                    weight=10.0)]
    draft = sync.compare(existing, [material(import_key="canvas:file:500")],
                         plan([planned()]), [cfile()])
    assert sync.nothing_to_do(draft) is True


def test_a_first_sync_of_a_new_class_has_something_to_do():
    draft = sync.compare([], [], plan([planned()]), [cfile()])
    assert sync.nothing_to_do(draft) is False


# ---------------------------------------------------------------------------
# rows as the database actually hands them over
# ---------------------------------------------------------------------------

def test_a_sqlite_row_works_the_same_as_a_dict():
    """`sqlite3.Row` has no `.get`, and these rows come straight out of a query."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE items (id TEXT, title TEXT, type TEXT, due_date TEXT,"
                 " due_time TEXT, weight REAL, score REAL, import_key TEXT, category_id TEXT)")
    conn.execute("INSERT INTO items VALUES ('row-1','Essay 1','assignment',NULL,NULL,"
                 "NULL,NULL,'canvas:100',NULL)")
    rows = conn.execute("SELECT * FROM items").fetchall()
    out = sync.compare_items(rows, plan([planned()]))
    assert out[0]["existingId"] == "row-1"
    assert out[0]["include"] is True


def test_a_row_missing_a_column_a_migration_has_not_added_yet_is_survivable():
    """`materials.import_key` is new. A row read before that migration ran has no such
    key, and the name match has to carry it."""
    out = sync.compare_files([{"id": "mat-1", "filename": "Lecture 1.pdf", "size": 1024}],
                             [cfile()])
    assert out[0]["change"] == "same"


def test_the_two_importers_agree_about_when_two_titles_are_the_same():
    import syllabus_import

    for title in ("Essay #1", "ESSAY 1", "essay  1"):
        assert sync.norm(title) == syllabus_import.import_key(title, "").rstrip("|")


def test_every_planned_type_survives_the_round_trip():
    """A type `canvas.py` produces has to be one the comparison can store."""
    import syllabus

    for _, typ in canvas.TYPE_WORDS:
        out = sync.compare_items([], plan([planned(type=typ)]))
        assert out[0]["type"] in syllabus.ITEM_TYPES

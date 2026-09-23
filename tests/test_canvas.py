"""Canvas: due dates land on the right day, weights come from the right mode, an
ungraded assignment is not a zero, and a course with its Files tab off still gives up
its files.

All of this runs with no network and no token: the pure layer is called directly, and
the three client tests stub `httpx`, which `canvas.py` imports inside its methods for
exactly this reason.
"""
import pytest

import canvas
import syllabus


# ---------------------------------------------------------------------------
# hosts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given", [
    "canvas.sfu.ca",
    "https://canvas.sfu.ca",
    "https://canvas.sfu.ca/",
    "http://canvas.sfu.ca/courses/12345",
    "  https://CANVAS.SFU.CA/courses/12345/assignments  ",
])
def test_host_comes_out_bare(given):
    """Whatever was in the address bar, only the host is kept."""
    assert canvas.normalise_host(given) == "canvas.sfu.ca"


def test_no_host_is_empty_not_an_error():
    assert canvas.normalise_host("") == ""
    assert canvas.normalise_host(None) == ""


# ---------------------------------------------------------------------------
# due dates
# ---------------------------------------------------------------------------

def test_an_evening_deadline_stays_on_its_own_day():
    """The bug this function exists to prevent.

    11:59pm Tuesday in Vancouver is 06:59 Wednesday UTC, which is what Canvas sends.
    Splitting the string would file it on Wednesday.
    """
    assert canvas.due_local("2026-09-30T06:59:59Z") == ("2026-09-29", "23:59")


def test_a_midday_deadline_converts_too():
    assert canvas.due_local("2026-09-30T19:00:00Z") == ("2026-09-30", "12:00")


def test_a_winter_deadline_uses_the_winter_offset():
    """January is PST, an hour further from UTC than September's PDT, so a fixed offset
    would put one of the two an hour out."""
    assert canvas.due_local("2026-01-15T07:59:00Z") == ("2026-01-14", "23:59")


def test_the_conversion_uses_the_same_zone_as_the_rest_of_the_app():
    """Deliberately not a hard-coded offset.

    The timezone database is political: this machine's copy has America/Vancouver
    leaving daylight saving for good on 2026-11-01, and a container with older data
    disagrees. What must hold is that a Canvas due date is read in the same zone
    `ics.py` and `gcal.py` use, whatever that zone currently says, because a suite that
    asserts an offset fails on a tzdata update rather than on a real bug.
    """
    import ics
    from datetime import datetime

    raw = "2026-11-25T07:59:00+00:00"
    expected = datetime.fromisoformat(raw).astimezone(ics.zone())
    assert canvas.due_local(raw) == (expected.strftime("%Y-%m-%d"), expected.strftime("%H:%M"))


@pytest.mark.parametrize("given", ["", None, "not a date", "2026-13-45T99:99:99Z"])
def test_a_missing_or_broken_due_date_is_simply_absent(given):
    assert canvas.due_local(given) == (None, None)


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------

def test_the_title_decides_before_the_group_does():
    a = {"name": "Midterm 1"}
    assert canvas.item_type(a, "Assignments") == "exam"


def test_the_group_decides_when_the_title_says_nothing():
    assert canvas.item_type({"name": "Week 3"}, "Quizzes") == "quiz"


def test_a_longer_phrase_wins_over_the_word_inside_it():
    """'final exam' must not be read as whatever 'final' alone would mean."""
    assert canvas.item_type({"name": "Final Exam"}, "") == "exam"
    assert canvas.item_type({"name": "Reading Response 2"}, "") == "reading"


@pytest.mark.parametrize("title,expected", [
    # straight out of his five courses
    ("Read Noba Attention & Failures of Awareness", "reading"),
    ("Read Designing with Mind in Mind ch 1", "reading"),
    ("In-lecture midterm exam 1", "exam"),
    ("In-class quiz 1", "quiz"),
    ("Final examination", "exam"),
    ("Attendance Diary 1", "other"),
    ("Quiz 02 - The Tree of Life", "quiz"),
    ("Writing Skill 1 - Create an Outline", "assignment"),
    ("Video Assignment- What You Have Learned", "assignment"),
])
def test_his_own_assignment_titles_come_out_right(title, expected):
    assert canvas.item_type({"name": title}, "") == expected


def test_a_reading_quiz_is_a_quiz():
    """'read' is in the vocabulary for his reading assignments, and must not outrank
    the word that says what the work actually is."""
    assert canvas.item_type({"name": "Reading quiz 3"}, "") == "quiz"


def test_canvas_own_flags_are_the_last_resort():
    assert canvas.item_type({"name": "Week 4", "quiz_id": 88}, "") == "quiz"
    assert canvas.item_type({"name": "Week 4", "submission_types": ["online_quiz"]}, "") == "quiz"
    assert canvas.item_type({"name": "Week 4", "submission_types": ["discussion_topic"]}, "") == "discussion"


def test_anything_else_is_an_assignment():
    assert canvas.item_type({"name": "Week 4", "submission_types": ["online_upload"]}, "") == "assignment"
    assert canvas.item_type({}, "") == "assignment"


def test_every_type_this_module_can_produce_is_one_vesta_knows():
    """The consistency `canvas.py` cannot get from importing `syllabus.py`.

    An assignment read off Canvas and the same assignment read off a syllabus PDF have
    to end up with the same type, and a type outside this list would not render.
    """
    for _, typ in canvas.TYPE_WORDS:
        assert typ in syllabus.ITEM_TYPES
    for produced in ("quiz", "discussion", "assignment"):
        assert produced in syllabus.ITEM_TYPES


# ---------------------------------------------------------------------------
# scores
# ---------------------------------------------------------------------------

def test_a_graded_submission_becomes_a_percent():
    """Vesta stores a percent, because the grade maths is weight * score / 100."""
    assert canvas.score_percent({"workflow_state": "graded", "score": 18}, 20) == 90.0


def test_a_real_zero_is_kept():
    assert canvas.score_percent({"workflow_state": "graded", "score": 0}, 20) == 0.0


@pytest.mark.parametrize("submission", [
    {"workflow_state": "submitted", "score": None},
    {"workflow_state": "pending_review", "score": None},
    {"workflow_state": "unsubmitted", "score": None},
    {"workflow_state": "graded", "score": None},
    {"workflow_state": "graded", "score": 10, "excused": True},
    {},
    None,
])
def test_anything_not_yet_marked_is_empty_rather_than_zero(submission):
    """A zero here would quietly drag the calculated grade down for weeks."""
    assert canvas.score_percent(submission, 20) is None


@pytest.mark.parametrize("points", [0, None, "", "abc"])
def test_a_score_out_of_nothing_is_not_a_percent(points):
    assert canvas.score_percent({"workflow_state": "graded", "score": 5}, points) is None


# ---------------------------------------------------------------------------
# weighting
# ---------------------------------------------------------------------------

WEIGHTED_COURSE = {"id": 1, "apply_assignment_group_weights": True}
POINTS_COURSE = {"id": 1, "apply_assignment_group_weights": False}

GROUPS = [
    {"id": 10, "name": "Assignments", "group_weight": 40, "rules": {"drop_lowest": 1},
     "assignments": [
         {"id": 100, "name": "Assignment 1", "points_possible": 10, "published": True},
         {"id": 101, "name": "Assignment 2", "points_possible": 30, "published": True},
     ]},
    {"id": 11, "name": "Final Exam", "group_weight": 60, "assignments": [
        {"id": 102, "name": "Final Exam", "points_possible": 60, "published": True},
    ]},
]


def test_a_weighted_course_becomes_categories():
    """A Canvas group is a Vesta grade category, which already shares its weight
    across the items in it. Nothing is divided up by hand."""
    cats, _ = canvas.weighting(WEIGHTED_COURSE, GROUPS)
    assert [(c["name"], c["weight"]) for c in cats] == [("Assignments", 40.0)]
    assert cats[0]["dropLowest"] == 1


def test_a_group_holding_one_assignment_is_just_that_assignment_s_weight():
    """REM388 files each assignment in a group of its own and ends up with eleven
    groups, seven of them holding one item. A one-item category and a plain weight
    behave identically in Vesta, so the flat one is the readable one."""
    cats, item_weights = canvas.weighting(WEIGHTED_COURSE, GROUPS)
    assert "Final Exam" not in [c["name"] for c in cats]
    assert item_weights == {102: 60.0}


def test_a_group_with_nothing_in_it_is_not_a_category():
    """SD381 carries "Imported Assignments" and "Unused assignments for this offering",
    both empty and both at 0%. They are the instructor's leftovers."""
    groups = GROUPS + [
        {"id": 12, "name": "Imported Assignments", "group_weight": 0, "assignments": []},
        {"id": 13, "name": "Unused assignments", "group_weight": 0},
        {"id": 14, "name": "All unpublished", "group_weight": 0, "assignments": [
            {"id": 103, "name": "Draft", "points_possible": 10, "published": False}]},
    ]
    cats, _ = canvas.weighting(WEIGHTED_COURSE, groups)
    assert [c["name"] for c in cats] == ["Assignments"]


def test_a_points_course_gets_item_weights_and_no_categories():
    """With the course flag off, every group_weight in the response is meaningless and
    importing them would wreck the Grades tab."""
    cats, item_weights = canvas.weighting(POINTS_COURSE, GROUPS)
    assert cats == []
    assert item_weights == {100: 10.0, 101: 30.0, 102: 60.0}
    assert round(sum(item_weights.values()), 2) == 100.0


def test_an_assignment_that_does_not_count_takes_no_share_of_the_weight():
    groups = [{"id": 10, "name": "Work", "group_weight": 100, "assignments": [
        {"id": 100, "name": "Practice quiz", "points_possible": 50, "published": True,
         "omit_from_final_grade": True},
        {"id": 101, "name": "Unpublished draft", "points_possible": 50, "published": False},
        {"id": 102, "name": "The real one", "points_possible": 25, "published": True},
    ]}]
    _, item_weights = canvas.weighting(POINTS_COURSE, groups)
    assert item_weights == {102: 100.0}


def test_a_course_worth_no_points_gets_no_invented_weights():
    groups = [{"id": 10, "name": "Work", "assignments": [
        {"id": 100, "name": "Ungraded survey", "points_possible": 0, "published": True},
    ]}]
    assert canvas.weighting(POINTS_COURSE, groups) == ([], {})


# ---------------------------------------------------------------------------
# the draft
# ---------------------------------------------------------------------------

def test_a_plan_carries_a_category_or_a_weight_but_never_both():
    """Both would count the same assignment towards the grade twice."""
    for course in (WEIGHTED_COURSE, POINTS_COURSE):
        for item in canvas.plan_course(course, GROUPS)["items"]:
            assert (item["categoryCanvasId"] is None) != (item["weight"] is None)


def test_a_points_course_says_its_weights_are_not_to_be_trusted():
    """The review screen leaves a weight tick off when this is False.

    IAT201 has twenty-five assignments all worth zero points, and PSYC300W has five set
    up out of a term's worth, which makes its first one 62.5% of the grade. Both
    figures follow correctly from Canvas and both are wrong about the course.
    """
    assert canvas.plan_course(POINTS_COURSE, GROUPS)["weightsFromCanvas"] is False
    assert canvas.plan_course(WEIGHTED_COURSE, GROUPS)["weightsFromCanvas"] is True


def test_a_plan_keys_every_item_for_re_sync():
    plan = canvas.plan_course(WEIGHTED_COURSE, GROUPS)
    assert [i["importKey"] for i in plan["items"]] == ["canvas:100", "canvas:101", "canvas:102"]


def test_a_plan_reads_the_title_type_due_date_and_score():
    groups = [{"id": 10, "name": "Assignments", "group_weight": 100, "assignments": [{
        "id": 100, "name": "Midterm 1", "points_possible": 40, "published": True,
        "due_at": "2026-10-14T06:59:00Z", "html_url": "https://canvas.sfu.ca/courses/1/assignments/100",
        "description": "<p>Covers <b>weeks 1 to 5</b>.</p>",
        "submission": {"workflow_state": "graded", "score": 34},
    }]}]
    item = canvas.plan_course(WEIGHTED_COURSE, groups)["items"][0]
    assert item["title"] == "Midterm 1"
    assert item["type"] == "exam"
    assert (item["dueDate"], item["dueTime"]) == ("2026-10-13", "23:59")
    assert item["notes"] == "Covers weeks 1 to 5."
    assert item["score"] == 85.0
    assert item["url"].endswith("/assignments/100")


def test_an_untitled_assignment_still_has_a_title():
    groups = [{"id": 10, "name": "Work", "assignments": [{"id": 100, "points_possible": 1}]}]
    assert canvas.plan_course(POINTS_COURSE, groups)["items"][0]["title"] == "Untitled"


# ---------------------------------------------------------------------------
# mapping courses onto classes
# ---------------------------------------------------------------------------

def test_a_course_code_matches_its_class_through_the_section_suffix():
    courses = [{"id": 1, "course_code": "CMPT 120 D100", "name": "Intro to Computing"}]
    classes = [{"id": "abc", "code": "CMPT 120", "name": "Intro to Computing"}]
    assert canvas.suggest_mapping(courses, classes)[0]["classId"] == "abc"


def test_the_sfu_prefix_and_dashes_do_not_stop_a_match():
    courses = [{"id": 1, "course_code": "SFU-COGS-300", "name": "Modelling"}]
    classes = [{"id": "abc", "code": "cogs 300"}]
    assert canvas.suggest_mapping(courses, classes)[0]["classId"] == "abc"


def test_an_ambiguous_code_suggests_nothing():
    """A silent wrong pairing files a whole term under the wrong class."""
    courses = [{"id": 1, "course_code": "CMPT 120 D100"}]
    classes = [{"id": "a", "code": "CMPT 120"}, {"id": "b", "code": "CMPT 120"}]
    assert canvas.suggest_mapping(courses, classes)[0]["classId"] is None


def test_an_unmatched_course_still_comes_back_to_be_asked_about():
    courses = [{"id": 7, "course_code": "PHIL 100", "name": "Knowledge and Reality",
                "term": {"name": "Fall 2026"}}]
    row = canvas.suggest_mapping(courses, [{"id": "a", "code": "CMPT 120"}])[0]
    assert row == {"courseId": 7, "label": "PHIL 100", "name": "Knowledge and Reality",
                   "term": "Fall 2026", "classId": None}


# ---------------------------------------------------------------------------
# files behind modules
# ---------------------------------------------------------------------------

MODULES = [
    {"name": "Week 1", "items": [
        {"type": "File", "content_id": 500, "title": "Lecture 1.pdf"},
        {"type": "Page", "content_id": 999, "title": "Welcome"},
        {"type": "SubHeader", "title": "Readings"},
    ]},
    {"name": "Week 2", "items": [
        {"type": "File", "content_id": 501, "title": "Lecture 2.pdf"},
        {"type": "File", "content_id": 500, "title": "Lecture 1.pdf (again)"},
    ]},
]


def test_only_files_come_out_of_the_modules_and_each_one_once():
    found = canvas.files_from_modules(MODULES)
    assert [f["fileId"] for f in found] == [500, 501]
    assert found[0]["module"] == "Week 1"
    assert found[1]["module"] == "Week 2"


def test_no_modules_is_no_files():
    assert canvas.files_from_modules([]) == []
    assert canvas.files_from_modules(None) == []


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------

def test_the_next_page_comes_out_of_the_link_header():
    header = ('<https://canvas.sfu.ca/api/v1/courses?page=1>; rel="current",'
              '<https://canvas.sfu.ca/api/v1/courses?page=2>; rel="next",'
              '<https://canvas.sfu.ca/api/v1/courses?page=9>; rel="last"')
    assert canvas.next_link(header) == "https://canvas.sfu.ca/api/v1/courses?page=2"


@pytest.mark.parametrize("header", [
    "", None,
    '<https://canvas.sfu.ca/api/v1/courses?page=1>; rel="current"',
    '<https://canvas.sfu.ca/api/v1/courses?page=9>; rel="last"',
])
def test_the_last_page_says_so_by_having_no_next(header):
    assert canvas.next_link(header) is None


# ---------------------------------------------------------------------------
# the client, with httpx stubbed
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=""):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text
        self.content = b"x" if body is not None else b""

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeCanvas:
    """Answers by URL, and records what was asked."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, headers=None, params=None, **kw):
        self.calls.append((url, params))
        for pattern, response in self.routes.items():
            if pattern in url:
                return response(self) if callable(response) else response
        return FakeResponse(404, {"errors": [{"message": "not found"}]})


@pytest.fixture
def stub(monkeypatch):
    def install(routes):
        fake = FakeCanvas(routes)
        monkeypatch.setattr("httpx.get", fake.get)
        return fake
    return install


def client():
    return canvas.Client("canvas.sfu.ca", "token-123")


def test_a_client_needs_a_host_and_a_token():
    with pytest.raises(canvas.CanvasError):
        canvas.Client("", "token")
    with pytest.raises(canvas.CanvasError) as caught:
        canvas.Client("canvas.sfu.ca", "  ")
    assert caught.value.needs_token


def test_a_rejected_token_says_so_in_a_way_the_screen_can_act_on(stub):
    stub({"/courses": FakeResponse(401, {"errors": [{"message": "Invalid access token."}]})})
    with pytest.raises(canvas.CanvasError) as caught:
        client().courses()
    assert caught.value.needs_token
    assert caught.value.status == 401


def test_canvas_own_words_reach_the_error(stub):
    stub({"/courses": FakeResponse(422, {"errors": [{"message": "Nope, not that."}]})})
    with pytest.raises(canvas.CanvasError) as caught:
        client().courses()
    assert "Nope, not that." in caught.value.message


def test_every_page_is_followed(stub):
    page2 = "https://canvas.sfu.ca/api/v1/courses?page=2"
    stub({
        "page=2": FakeResponse(200, [{"id": 3}]),
        "/courses": FakeResponse(200, [{"id": 1}, {"id": 2}],
                                 {"link": "<%s>; rel=\"next\"" % page2}),
    })
    assert [c["id"] for c in client().courses()] == [1, 2, 3]


def test_pagination_stops_even_if_canvas_never_does(stub):
    """A next link that always points somewhere new would otherwise spin forever."""
    forever = FakeResponse(200, [{"id": 1}],
                           {"link": '<https://canvas.sfu.ca/api/v1/courses?page=x>; rel="next"'})
    fake = stub({"/courses": forever})
    client().courses()
    assert len(fake.calls) == canvas.MAX_PAGES


def test_a_hidden_files_tab_falls_back_to_the_modules(stub):
    """A 403 on /files means the instructor switched the tab off, not that there are
    no files. Saif asked for this case specifically."""
    stub({
        "/files/500": FakeResponse(200, {"id": 500, "display_name": "Lecture 1.pdf"}),
        "/files/501": FakeResponse(200, {"id": 501, "display_name": "Lecture 2.pdf"}),
        "/courses/1/files": FakeResponse(403, {"status": "unauthorized"}),
        "/courses/1/modules": FakeResponse(200, MODULES),
    })
    files = client().course_files(1)
    assert [f["display_name"] for f in files] == ["Lecture 1.pdf", "Lecture 2.pdf"]
    assert [f["_module"] for f in files] == ["Week 1", "Week 2"]


def test_a_visible_files_tab_is_used_directly(stub):
    fake = stub({"/courses/1/files": FakeResponse(200, [{"id": 500, "display_name": "a.pdf"}])})
    files = client().course_files(1)
    assert [f["display_name"] for f in files] == ["a.pdf"]
    assert not any("modules" in url for url, _ in fake.calls)


def test_one_unreadable_file_does_not_lose_the_others(stub):
    """A file locked until a later date 403s on its own. The rest of the course still
    imports."""
    stub({
        "/files/500": FakeResponse(403, {"status": "unauthorized"}),
        "/files/501": FakeResponse(200, {"id": 501, "display_name": "Lecture 2.pdf"}),
        "/courses/1/files": FakeResponse(403, {"status": "unauthorized"}),
        "/courses/1/modules": FakeResponse(200, MODULES),
    })
    assert [f["display_name"] for f in client().course_files(1)] == ["Lecture 2.pdf"]


def test_a_throttle_is_not_mistaken_for_a_hidden_files_tab(stub):
    """Canvas answers both with 403. Read the wrong way, a rate limit would report a
    course as having no files at all."""
    stub({"/courses/1/files": FakeResponse(403, None, {},
                                           text="403 Forbidden (Rate Limit Exceeded)")})
    with pytest.raises(canvas.CanvasError) as caught:
        client().course_files(1)
    assert caught.value.rate_limited
    assert "rate limiting" in caught.value.message


def test_an_exhausted_rate_limit_header_is_also_a_throttle(stub):
    stub({"/courses": FakeResponse(403, None, {"x-rate-limit-remaining": "0"})})
    with pytest.raises(canvas.CanvasError) as caught:
        client().courses()
    assert caught.value.rate_limited


def test_a_throttle_partway_through_the_modules_stops_rather_than_half_reporting(stub):
    """Every call after a throttle fails the same way, so returning what was collected
    so far would look like a course that has lost most of its files."""
    stub({
        "/files/500": FakeResponse(403, None, {}, text="403 Forbidden (Rate Limit Exceeded)"),
        "/courses/1/files": FakeResponse(403, {"status": "unauthorized"}),
        "/courses/1/modules": FakeResponse(200, MODULES),
    })
    with pytest.raises(canvas.CanvasError) as caught:
        client().course_files(1)
    assert caught.value.rate_limited


def test_folders_are_empty_rather_than_an_error_when_the_tab_is_off(stub):
    stub({"/courses/1/folders": FakeResponse(403, {"status": "unauthorized"})})
    assert client().folders(1) == []


def test_a_file_over_the_ceiling_is_refused_before_it_is_downloaded(tmp_path, monkeypatch):
    """Declared by Content-Length, so nothing is written at all."""
    class Stream:
        def __enter__(self):
            return FakeResponse(200, None, {"content-length": str(300 * 1024 * 1024)})

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("httpx.stream", lambda *a, **kw: Stream())
    dest = tmp_path / "big.pdf"
    with pytest.raises(canvas.CanvasError) as caught:
        client().download("https://canvas.sfu.ca/files/1/download", str(dest))
    assert "left as a link" in caught.value.message
    assert not dest.exists()


def test_a_file_that_grows_past_the_ceiling_mid_stream_leaves_nothing_behind(tmp_path, monkeypatch):
    """A chunked response has no length to check up front, so the bytes are counted as
    they arrive and a half-written file is removed rather than mistaken for a whole
    one."""
    class Streamed(FakeResponse):
        def iter_bytes(self):
            for _ in range(5):
                yield b"a" * 40

    class Stream:
        def __enter__(self):
            return Streamed(200, None, {})

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("httpx.stream", lambda *a, **kw: Stream())
    dest = tmp_path / "big.pdf"
    with pytest.raises(canvas.CanvasError):
        client().download("https://canvas.sfu.ca/files/1/download", str(dest), max_bytes=100)
    assert not dest.exists()


def test_a_download_writes_the_file_and_reports_its_size(tmp_path, monkeypatch):
    class Streamed(FakeResponse):
        def iter_bytes(self):
            yield b"%PDF-1.4"
            yield b" rest"

    class Stream:
        def __enter__(self):
            return Streamed(200, None, {"content-length": "13"})

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("httpx.stream", lambda *a, **kw: Stream())
    dest = tmp_path / "ok.pdf"
    assert client().download("https://canvas.sfu.ca/files/1/download", str(dest)) == 13
    assert dest.read_bytes() == b"%PDF-1.4 rest"


# ---------------------------------------------------------------------------
# names for a class made from a course
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,expected", [
    ("SD381 OL01", "SD 381"),          # the section is noise on a class card
    ("PHIL110 D100", "PHIL 110"),
    ("PSYC300W D100", "PSYC 300W"),    # a W course keeps its W
    ("SFU-COGS-300", "COGS 300"),
    ("Orientation 2026", "Orientation 2026"),   # not subject-plus-number: left alone
])
def test_a_new_class_gets_the_code_a_student_would_write(code, expected):
    assert canvas.class_code({"course_code": code}) == expected


def test_a_course_name_loses_the_code_canvas_repeats_in_it():
    course = {"course_code": "PHIL110 D100",
              "name": "PHIL110 D100 Introduction to Logic and Reasoning"}
    assert canvas.course_title(course) == "Introduction to Logic and Reasoning"
    assert canvas.suggest_mapping([dict(course, id=1)], [])[0]["name"] == \
        "Introduction to Logic and Reasoning"


def test_a_nul_byte_never_reaches_postgres():
    """Postgres refuses NUL in text, and one in a description would fail the whole
    apply on the deployment while SQLite let it through everywhere else."""
    plan = canvas.plan_course(POINTS_COURSE, [{"id": 1, "name": "W", "assignments": [
        {"id": 5, "name": "Essay\x00 1", "description": "<p>Due\x00 soon</p>",
         "points_possible": 1}]}])
    item = plan["items"][0]
    assert "\x00" not in item["title"] and "\x00" not in item["notes"]

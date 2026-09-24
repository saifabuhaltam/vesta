"""This Week: his real Fall 2026 modules, split into weeks the way each course means them.

The fixture is what Canvas returned for his five courses on 2026-09-24, reduced to the
fields `week.reduce_modules` keeps. Each test is a rule from WEEK.md, checked against
the course that made it necessary.
"""
import json
import os
from datetime import date

import pytest

import ics
import week

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "modules_fall2026.json")
TERM_START = date(2026, 9, 9)
TZ = ics.zone("America/Los_Angeles")


@pytest.fixture(scope="module")
def modules():
    with open(FIXTURE) as f:
        return json.load(f)


def entries(modules, code):
    return week.course_entries(code, modules[code], TERM_START, TZ)


def in_week(result, n):
    return [e for e in result["entries"] if e["week"] == n]


def titles(rows, task=None):
    return [e["title"] for e in rows if task is None or e["task"] == task]


# ---------------------------------------------------------------------------
# dates and weeks
# ---------------------------------------------------------------------------

def test_week_one_is_the_week_holding_the_first_day():
    assert week.week_number(date(2026, 9, 9), TERM_START) == 1
    assert week.week_number(date(2026, 9, 7), TERM_START) == 1
    assert week.week_number(date(2026, 9, 21), TERM_START) == 3
    assert week.week_start(3, TERM_START) == date(2026, 9, 21)


def test_dates_are_read_both_ways_round():
    assert week.dates_in("READINGS (do before Sep. 22 class)", TERM_START) == [date(2026, 9, 22)]
    assert week.dates_in("Week 07 - Midterm - Monday, 19 Oct - 14:30", TERM_START) == [date(2026, 10, 19)]
    assert week.dates_in("due Oct. 4th", TERM_START) == [date(2026, 10, 4)]
    assert week.dates_in("Lecture 03 - Video 04", TERM_START) == []


def test_a_january_date_in_a_fall_term_is_next_year():
    assert week.dates_in("Jan 5", TERM_START) == [date(2027, 1, 5)]


# ---------------------------------------------------------------------------
# splitting and offsets, per course
# ---------------------------------------------------------------------------

def test_psyc_numbers_its_weeks_one_behind_the_calendar(modules):
    r = entries(modules, "PSYC300W")
    assert (r["offset"], r["offsetSource"]) == (1, "written")
    # Its "Week 2" readings are "do before Sep. 22 class": calendar week 3.
    assert "The End of Reading Is Here - The Atlantic" in titles(in_week(r, 3))
    assert r["weeks"][3]["topic"] == "Our Changing Relationship with the Written Word"


def test_rem_is_on_the_calendar_because_its_midterm_says_so(modules):
    r = entries(modules, "REM388")
    assert (r["offset"], r["offsetSource"]) == (0, "written")


def test_a_date_for_something_later_does_not_move_a_course(modules):
    # SD 381's Week 2 says "upcoming video assignment due Oct. 4th". Read as the week's
    # own date, it shifted every SD 381 reading two weeks late.
    r = entries(modules, "SD381")
    assert r["offset"] == 0


def test_iat_weeks_come_from_subheaders_inside_themed_modules(modules):
    r = entries(modules, "IAT201")
    assert r["offset"] == 0
    wk3 = in_week(r, 3)
    assert "Read Designing With the Mind in Mind Ch 1-3" in titles(wk3, task=True)
    assert r["weeks"][3]["topic"] == "Designing for the Brain"
    # Nothing from "Course Overview (Start here!)" is in any week.
    assert not any("HCI Broker_V8" in e["title"] for e in r["entries"])


def test_phil_slides_named_by_week_are_links_in_that_week(modules):
    r = entries(modules, "PHIL110")
    wk1 = in_week(r, 1)
    assert [(e["title"], e["task"]) for e in wk1] == [("What is logic", False)]


# ---------------------------------------------------------------------------
# placing by due date
# ---------------------------------------------------------------------------

def test_a_dated_item_goes_in_the_week_it_is_due(modules):
    # In PSYC's "Week 3" module (calendar week 4 by its offset) but due Sep 23.
    r = entries(modules, "PSYC300W")
    ws2 = [e for e in r["entries"] if e["title"].startswith("Writing Skill 2")]
    assert ws2 and all(e["week"] == 3 for e in ws2)
    assert ws2[0]["dueDate"] == "2026-09-23"


def test_an_evening_deadline_is_that_evening_not_the_next_day(modules):
    r = entries(modules, "REM388")
    d3 = [e for e in r["entries"] if e["title"].startswith("Discussion 03")][0]
    assert (d3["dueDate"], d3["week"]) == ("2026-09-27", 3)


# ---------------------------------------------------------------------------
# checkbox or link
# ---------------------------------------------------------------------------

def test_psyc_readings_are_tasks_and_its_slides_are_links(modules):
    wk3 = in_week(entries(modules, "PSYC300W"), 3)
    kinds = {e["title"]: (e["kind"], e["task"]) for e in wk3}
    assert kinds["Postman 1985 - Amusing Ourselves to Death - Ch. 2 - Media as Epistemology"] == ("reading", True)
    assert kinds["PSYC 300W - Lecture 2 - The Written Word"] == ("link", False)
    assert kinds["In-Class Quiz 1"] == ("quiz", True)


def test_rem_handouts_are_links_and_lecture_videos_are_tasks(modules):
    wk4 = in_week(entries(modules, "REM388"), 4)
    tasks = titles(wk4, task=True)
    links = titles(wk4, task=False)
    assert "Lecture 03 - HANDOUT - Evolution by Natural Selection" in links
    assert "REM 388 Tut 03 Evolution Canvas" in links
    assert "Lecture 03 - Video 01 - Genome - DNA and the Nucleotides-G-C-A-T" in tasks


def test_iat_assignment_briefs_are_links_and_its_readings_are_tasks(modules):
    wk3 = in_week(entries(modules, "IAT201"), 3)
    by_title = {e["title"]: e for e in wk3}
    assert by_title["DDA1: Workflow Overview"]["task"] is False
    assert by_title["IAT201.3_Fall2026"]["task"] is False
    assert by_title["Read Noba Sensation & Perception and Vision in Cognition for Designers"]["kind"] == "reading"


def test_sd381_lecture_pages_are_tasks_and_their_prefix_is_dropped(modules):
    wk3 = in_week(entries(modules, "SD381"), 3)
    by_title = {e["title"]: e for e in wk3}
    assert by_title["Virtual Lecture Material and/or Activity- Part 1"]["task"] is True
    assert by_title["Summary"]["task"] is False


def test_a_file_linked_twice_in_one_week_is_one_row(modules):
    r = entries(modules, "IAT201")
    keys = [(e["week"], e["canvasType"], e["contentId"]) for e in r["entries"] if e["contentId"]]
    assert len(keys) == len(set(keys))


def test_canvas_completion_is_carried():
    # SD 381's week 3 overview is "must view", not yet viewed; a viewed one pre-ticks.
    mods = [{"name": "Week 3", "items": [
        {"id": 1, "type": "Page", "title": "Overview and Preparation Work",
         "completion_requirement": {"type": "must_view", "completed": True}},
        {"id": 2, "type": "Page", "title": "Virtual Lecture Material - Part 1",
         "completion_requirement": {"type": "must_view", "completed": False}}]}]
    r = week.course_entries("SD381", mods, TERM_START, TZ)
    assert [e["canvasDone"] for e in r["entries"]] == [True, False]


def test_a_hand_set_shift_replaces_the_guess(modules):
    r = week.course_entries("PSYC300W", modules["PSYC300W"], TERM_START, TZ, shift=0)
    assert (r["offset"], r["offsetSource"]) == (0, "set")
    assert "The End of Reading Is Here - The Atlantic" in titles(in_week(r, 2))


def test_keys_are_stable_across_checks(modules):
    a = entries(modules, "REM388")
    b = entries(modules, "REM388")
    assert [e["key"] for e in a["entries"]] == [e["key"] for e in b["entries"]]
    assert all(e["key"].startswith("canvas:REM388:mi:") for e in a["entries"])

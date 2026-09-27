"""ics.py on icalendar and recurring-ical-events: the shapes the hand-written parser
handled, and the ones it did not (monthly rules, RDATE, a moved occurrence)."""
from datetime import datetime

import ics

TZ = ics.zone("America/Vancouver")
END = datetime(2026, 12, 31, tzinfo=TZ)


def cal(*events):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def vevent(*lines):
    return "BEGIN:VEVENT\r\n" + "".join(l + "\r\n" for l in lines) + "END:VEVENT\r\n"


def dates(feed):
    [ev] = ics.parse(feed, TZ)
    return [(o["date"], o["start"]) for o in ics.occurrences(ev, END, TZ)]


def test_weekly_with_byday_until_in_utc_and_an_exdate():
    got = dates(cal(vevent(
        "UID:a", "SUMMARY:Lecture",
        "DTSTART;TZID=America/Vancouver:20260908T103000",
        "RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20260925T065959Z",
        "EXDATE;TZID=America/Vancouver:20260915T103000")))
    assert got == [("2026-09-08", "10:30"), ("2026-09-10", "10:30"), ("2026-09-17", "10:30"),
                   ("2026-09-22", "10:30"), ("2026-09-24", "10:30")]


def test_utc_times_land_on_the_local_date():
    [ev] = ics.parse(cal(vevent("UID:b", "SUMMARY:Late", "DTSTART:20261002T050000Z")), TZ)
    assert (ev["date"], ev["start"]) == ("2026-10-01", "22:00")


def test_text_is_unescaped_and_unfolded():
    [ev] = ics.parse(cal(vevent("UID:c", "SUMMARY:Room 101\\, AQ", "DESCRIPTION:line one\\nline two",
                                "LOCATION:a long place name that", "  continues", "DTSTART;VALUE=DATE:20261015",
                                "STATUS:CANCELLED")), TZ)
    assert ev["title"] == "Room 101, AQ"
    assert ev["description"] == "line one\nline two"
    assert ev["location"] == "a long place name that continues"
    assert ev["allDay"] and ev["cancelled"] and ev["date"] == "2026-10-15"


def test_monthly_by_weekday_the_old_parser_cut_to_one():
    got = dates(cal(vevent("UID:d", "SUMMARY:Club", "DTSTART;TZID=America/Vancouver:20260908T180000",
                           "RRULE:FREQ=MONTHLY;BYDAY=2TU;COUNT=4")))
    assert [d for d, _ in got] == ["2026-09-08", "2026-10-13", "2026-11-10", "2026-12-08"]


def test_rdate_adds_an_occurrence():
    got = dates(cal(vevent("UID:e", "SUMMARY:Rent", "DTSTART;VALUE=DATE:20261001",
                           "RRULE:FREQ=MONTHLY;BYMONTHDAY=1", "RDATE;VALUE=DATE:20261015")))
    assert [d for d, _ in got] == ["2026-10-01", "2026-10-15", "2026-11-01", "2026-12-01"]


def test_a_moved_occurrence_replaces_the_original_and_is_not_listed_twice():
    feed = cal(
        vevent("UID:f", "SUMMARY:Tutorial", "DTSTART;TZID=America/Vancouver:20260909T140000",
               "RRULE:FREQ=WEEKLY;COUNT=3"),
        vevent("UID:f", "RECURRENCE-ID;TZID=America/Vancouver:20260916T140000",
               "SUMMARY:Tutorial (moved)", "DTSTART;TZID=America/Vancouver:20260917T160000"))
    evs = ics.parse(feed, TZ)
    assert len(evs) == 1
    got = [(o["date"], o["start"], o["title"]) for o in ics.occurrences(evs[0], END, TZ)]
    assert got == [("2026-09-09", "14:00", "Tutorial"), ("2026-09-17", "16:00", "Tutorial (moved)"),
                   ("2026-09-23", "14:00", "Tutorial")]


def test_a_broken_feed_is_an_empty_list_not_an_error():
    assert ics.parse("not a calendar", TZ) == []

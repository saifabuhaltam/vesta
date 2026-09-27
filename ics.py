"""Reading iCalendar text, on icalendar and recurring-ical-events.

This used to be a hand-written parser, chosen to avoid a dependency. Its recurrence
expansion only knew FREQ=DAILY and FREQ=WEEKLY and returned the first occurrence of
anything else, so a monthly or yearly event, an RDATE, or a single moved occurrence
(RECURRENCE-ID) came through wrong. Saif chose to replace it with the libraries:
icalendar (BSD-2) parses the file, including line folding, escaping and VTIMEZONE,
and recurring-ical-events (LGPL-3, used unmodified) expands every RRULE form, RDATE,
EXDATE and moved occurrences.

The output is the same as before: `parse` gives one plain dict per event and
`occurrences` flattens a recurring one into a dict per date, with the same keys and
the same fingerprint. Nothing calls these yet: Vesta's calendar data comes from the
Canvas and Google Calendar APIs and SFU's course outlines. They are here for a feed
subscription, and `zone()` is what the rest of the app uses.

Deliberately forgiving still: an event that cannot be read is skipped, not raised,
because a half-read feed on the review screen beats an error.
"""
import hashlib
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover - 3.9+ has it
    ZoneInfo = None

LOCAL_TZ = "America/Vancouver"

MAX_OCCURRENCES = 400          # a guard against COUNT=999999 and unbounded rules


def zone(name=LOCAL_TZ):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _local(value, tz):
    """A DTSTART-style value in `tz`, so the date it lands on is the date the student
    sees. A bare date stays a date; a floating time is read as local time."""
    if value is None or not isinstance(value, datetime) or tz is None:
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def as_fields(dt, all_day):
    """A datetime or date -> the ('YYYY-MM-DD', 'HH:MM') pair Vesta stores."""
    if dt is None:
        return None, None
    if all_day or isinstance(dt, date) and not isinstance(dt, datetime):
        return dt.isoformat()[:10], None
    return dt.date().isoformat(), dt.strftime("%H:%M")


def event_hash(ev):
    """A stable fingerprint of the parts a student would notice changing."""
    parts = [ev.get("title", ""), ev.get("date") or "", ev.get("start") or "",
             ev.get("end") or "", ev.get("location", ""), ev.get("description", "")[:500]]
    return hashlib.sha256("␟".join(parts).encode("utf-8")).hexdigest()[:32]


def _text(component, name):
    value = component.get(name)
    return str(value) if value is not None else ""


def _event(component, tz, calendar=None):
    """One VEVENT (or one expanded occurrence of it) as Vesta's plain dict."""
    start_prop = component.get("DTSTART")
    if start_prop is None:
        return None
    raw_start = start_prop.dt
    all_day = not isinstance(raw_start, datetime)
    start = _local(raw_start, tz)
    end_prop = component.get("DTEND")
    end = _local(end_prop.dt, tz) if end_prop is not None else None
    date_s, time_s = as_fields(start, all_day)
    end_date, end_time = as_fields(end, all_day)
    ev = {
        "uid": _text(component, "UID"),
        "title": _text(component, "SUMMARY").strip(),
        "description": _text(component, "DESCRIPTION"),
        "location": _text(component, "LOCATION"),
        "url": _text(component, "URL"),
        "date": date_s,
        "start": time_s,
        "end": end_time if end_date == date_s else None,
        "allDay": all_day,
        "cancelled": _text(component, "STATUS").upper() == "CANCELLED",
        "rrule": dict(component["RRULE"]) if component.get("RRULE") is not None else None,
        "_start": start,
        "_calendar": calendar,
    }
    ev["hash"] = event_hash(ev)
    return ev


def parse(text, tz=None):
    """Every event in an ICS document, as plain dicts, one per VEVENT.

    A moved occurrence of a recurring event (a VEVENT with RECURRENCE-ID) is not
    listed on its own: `occurrences` puts it where it belongs in the series.
    """
    import icalendar

    tz = tz or zone()
    try:
        cal = icalendar.Calendar.from_ical(text or "")
    except Exception:
        return []
    events = []
    for component in cal.walk("VEVENT"):
        if component.get("RECURRENCE-ID") is not None:
            continue
        try:
            ev = _event(component, tz, cal)
        except Exception:
            continue
        if ev:
            events.append(ev)
    return events


def occurrences(ev, window_end, tz=None):
    """A recurring event flattened into one dict per date it actually falls on, from
    its first occurrence up to `window_end`. A one-off event comes back as itself."""
    import recurring_ical_events

    start = ev.get("_start")
    cal = ev.get("_calendar")
    if not ev.get("rrule") or start is None or cal is None:
        return [ev]
    tz = tz or zone()
    begin = start if isinstance(start, datetime) else datetime(start.year, start.month, start.day)
    end = window_end if isinstance(window_end, datetime) else datetime(
        window_end.year, window_end.month, window_end.day)
    end = end + timedelta(seconds=1)          # inclusive of window_end, as before
    if begin.tzinfo is None and tz is not None:
        begin = begin.replace(tzinfo=tz)
    if end.tzinfo is None and tz is not None:
        end = end.replace(tzinfo=tz)
    out = []
    try:
        for component in recurring_ical_events.of(cal).between(begin, end):
            if _text(component, "UID") != ev.get("uid"):
                continue
            one = _event(component, tz, cal)
            if one:
                one["rrule"] = ev.get("rrule")
                out.append(one)
            if len(out) >= MAX_OCCURRENCES:
                break
    except Exception:
        return [ev]
    out.sort(key=lambda o: (o["date"] or "", o["start"] or ""))
    return out or [ev]

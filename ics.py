"""Reading iCalendar text.

Vesta already wrote ICS by hand in `export_ics`; this reads it back, so a Canvas feed
or a Google export can come in the same door. A complete RFC 5545 implementation is
not the goal. This covers what real feeds actually emit and is deliberately forgiving:
a feed that half-parses is more useful than one that raises, because the review screen
shows the student what came through before anything is written.

Deliberately no new dependency. `icalendar` and `dateutil` would both pull a package
into a Python 3.9 venv that is already past end of life, to parse a format Vesta
generates itself.
"""
import hashlib
import re
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover - 3.9+ has it
    ZoneInfo = None

LOCAL_TZ = "America/Vancouver"

# ICS weekday codes to Python's Monday=0
BYDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
# ...and to the JS-style Sunday=0 that `schedule_entries.day` and the frontend use
JS_DAY = {"MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6, "SU": 0}

MAX_OCCURRENCES = 400          # a guard against COUNT=999999 and unbounded rules


def zone(name=LOCAL_TZ):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Lexing
# ---------------------------------------------------------------------------
def unfold(text):
    """Undo RFC 5545 line folding: a leading space or tab continues the line before."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def _split_unquoted(s, sep):
    """Split on `sep`, ignoring any that sits inside double quotes."""
    parts, buf, inq = [], [], False
    for c in s:
        if c == '"':
            inq = not inq
            buf.append(c)
        elif c == sep and not inq:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    parts.append("".join(buf))
    return parts


def split_line(line):
    """'DTSTART;TZID=America/Vancouver:20260909T143000' -> (name, params, value)."""
    head_value = _split_unquoted(line, ":")
    if len(head_value) < 2:
        return None, {}, ""
    head, value = head_value[0], ":".join(head_value[1:])
    bits = _split_unquoted(head, ";")
    params = {}
    for p in bits[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return bits[0].upper(), params, value


def unescape(v):
    """\\n is a newline; \\, \\; \\\\ are literals."""
    out, i = [], 0
    while i < len(v):
        if v[i] == "\\" and i + 1 < len(v):
            nxt = v[i + 1]
            out.append("\n" if nxt in ("n", "N") else nxt)
            i += 2
        else:
            out.append(v[i])
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def parse_dt(value, params, tz=None):
    """Return (datetime_or_date, all_day).

    Three shapes appear in the wild: a bare date (VALUE=DATE), a UTC instant ending
    in Z, and a local time with or without a TZID. Anything with a time is returned
    in `tz` so the date it lands on is the date the student sees.
    """
    tz = tz or zone()
    value = (value or "").strip()
    if not value:
        return None, False
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        try:
            return datetime.strptime(value[:8], "%Y%m%d").date(), True
        except ValueError:
            return None, False
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value)
    if not m:
        return None, False
    try:
        naive = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None, False
    if ZoneInfo is None or tz is None:
        return naive, False
    if m.group(3) == "Z":
        return naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz), False
    src = zone(params["TZID"]) if params.get("TZID") else None
    # no TZID and no Z is "floating": it means whatever local time the reader is in
    return (naive.replace(tzinfo=src).astimezone(tz) if src else naive.replace(tzinfo=tz)), False


def as_fields(dt, all_day):
    """A datetime or date -> the ('YYYY-MM-DD', 'HH:MM') pair Vesta stores."""
    if dt is None:
        return None, None
    if all_day or isinstance(dt, date) and not isinstance(dt, datetime):
        return dt.isoformat()[:10], None
    return dt.date().isoformat(), dt.strftime("%H:%M")


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------
def parse_rrule(value):
    out = {}
    for part in (value or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.upper()] = v
    return out


def _comparable(other, ref):
    """Return `other` in a form that can be compared with `ref` without raising.

    Dates and datetimes do not compare, and neither do aware and naive datetimes.
    Feeds mix all three freely, so bounds are coerced to match DTSTART rather than
    trusting them to agree.
    """
    if other is None:
        return None
    if not isinstance(ref, datetime):
        return other.date() if isinstance(other, datetime) else other
    if not isinstance(other, datetime):
        other = datetime(other.year, other.month, other.day)
    if ref.tzinfo is None and other.tzinfo is not None:
        return other.replace(tzinfo=None)
    if ref.tzinfo is not None and other.tzinfo is None:
        return other.replace(tzinfo=ref.tzinfo)
    return other


def expand_rrule(start, rule, window_end, exdates=()):
    """Occurrence start times for a recurring event, capped and bounded.

    Only FREQ=DAILY and FREQ=WEEKLY are expanded, since those are what a course
    timetable uses. Anything else yields the first occurrence alone rather than
    guessing, and the caller can say so on the review screen.
    """
    if not rule or not start:
        return [start] if start else []
    freq = rule.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY"):
        return [start]
    interval = max(1, int(rule.get("INTERVAL") or 1))
    count = int(rule["COUNT"]) if (rule.get("COUNT") or "").isdigit() else None

    until = None
    if rule.get("UNTIL"):
        until, _ = parse_dt(rule["UNTIL"], {}, getattr(start, "tzinfo", None) or zone())

    # One feed can mix all-day dates with zoned times, and UNTIL is usually UTC even
    # when DTSTART is not. Comparing those directly raises, so every bound is brought
    # onto the same footing as DTSTART before anything is compared.
    limit = _comparable(window_end, start)
    until = _comparable(until, start)

    days = [BYDAY[d] for d in (rule.get("BYDAY") or "").upper().split(",") if d in BYDAY]
    skip = {d.isoformat()[:10] if hasattr(d, "isoformat") else str(d) for d in exdates}

    def past(d):
        return (limit is not None and d > limit) or (until is not None and d > until)

    # COUNT limits what the rule *generates*; EXDATE removes occurrences afterwards.
    # Filtering first and then counting would invent a replacement for every excluded
    # date, quietly pushing a whole term of deadlines one meeting later.
    raw, guard = [], 0
    if freq == "DAILY":
        step, cursor = timedelta(days=interval), start
        while guard < MAX_OCCURRENCES and not past(cursor):
            raw.append(cursor)
            if count and len(raw) >= count:
                break
            cursor += step
            guard += 1
    else:
        # WEEKLY: walk week by week, emitting each named weekday inside the week
        week_start = start - timedelta(days=start.weekday())
        targets = sorted(days) or [start.weekday()]
        stop = False
        while guard < MAX_OCCURRENCES and not stop:
            for wd in targets:
                when = week_start + timedelta(days=wd)
                if when < start:
                    continue
                if past(when):
                    stop = True
                    break
                raw.append(when)
                if count and len(raw) >= count:
                    stop = True
                    break
            week_start += timedelta(weeks=interval)
            guard += 1
    return [d for d in raw if d.isoformat()[:10] not in skip]


# ---------------------------------------------------------------------------
# Parsing a calendar
# ---------------------------------------------------------------------------
TEXT_FIELDS = {"SUMMARY": "title", "DESCRIPTION": "description", "LOCATION": "location",
               "UID": "uid", "STATUS": "status", "URL": "url"}


def parse(text, tz=None):
    """Every VEVENT in an ICS document, as plain dicts.

    Unknown properties are ignored rather than collected: nothing downstream reads
    them, and keeping them invites treating a feed's private fields as stable.
    """
    tz = tz or zone()
    events, cur, in_event = [], None, False
    for line in unfold(text or ""):
        name, params, value = split_line(line)
        if name is None:
            continue
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur, in_event = {"exdates": []}, True
            continue
        if name == "END" and value.upper() == "VEVENT":
            if in_event and cur:
                events.append(_finish(cur, tz))
            cur, in_event = None, False
            continue
        if not in_event or cur is None:
            continue
        if name in TEXT_FIELDS:
            cur[TEXT_FIELDS[name]] = unescape(value)
        elif name == "DTSTART":
            cur["start"], cur["all_day"] = parse_dt(value, params, tz)
        elif name == "DTEND":
            cur["end"], _ = parse_dt(value, params, tz)
        elif name == "RRULE":
            cur["rrule"] = parse_rrule(value)
        elif name == "EXDATE":
            for v in value.split(","):
                d, _ = parse_dt(v, params, tz)
                if d is not None:
                    cur["exdates"].append(d)
        elif name == "LAST-MODIFIED":
            cur["modified"], _ = parse_dt(value, params, tz)
    return events


def _finish(cur, tz):
    start, all_day = cur.get("start"), bool(cur.get("all_day"))
    date_s, time_s = as_fields(start, all_day)
    end_date, end_time = as_fields(cur.get("end"), all_day)
    ev = {
        "uid": cur.get("uid") or "",
        "title": (cur.get("title") or "").strip(),
        "description": cur.get("description") or "",
        "location": cur.get("location") or "",
        "url": cur.get("url") or "",
        "date": date_s,
        "start": time_s,
        "end": end_time if end_date == date_s else None,
        "allDay": all_day,
        "cancelled": (cur.get("status") or "").upper() == "CANCELLED",
        "rrule": cur.get("rrule"),
        "exdates": cur.get("exdates") or [],
        "_start": start,
    }
    ev["hash"] = event_hash(ev)
    return ev


def event_hash(ev):
    """A stable fingerprint of the parts a student would notice changing."""
    parts = [ev.get("title", ""), ev.get("date") or "", ev.get("start") or "",
             ev.get("end") or "", ev.get("location", ""), ev.get("description", "")[:500]]
    return hashlib.sha256("␟".join(parts).encode("utf-8")).hexdigest()[:32]


def occurrences(ev, window_end, tz=None):
    """A recurring VEVENT flattened into one dict per date it actually falls on."""
    start = ev.get("_start")
    if not ev.get("rrule") or start is None:
        return [ev]
    out = []
    for when in expand_rrule(start, ev["rrule"], window_end, ev.get("exdates")):
        date_s, time_s = as_fields(when, ev.get("allDay"))
        copy = dict(ev, date=date_s, start=time_s, _start=when)
        copy["hash"] = event_hash(copy)
        out.append(copy)
    return out

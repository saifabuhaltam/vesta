"""Google Calendar: OAuth, and turning Vesta's work into events and back.

Split deliberately in two. Everything above "the transport" is pure: it maps a Vesta
item or class meeting to a Google event body and back, and can be tested with no
network and no credential. Only the client below actually talks to Google, so the part
that is hard to test is also the part with almost no logic in it.

No `google-auth`, no `googleapiclient`. Google Calendar is a REST/JSON API and an OAuth
refresh is a single POST, so `httpx` (already present, it ships with `anthropic`) is the
entire dependency. That matters here: this venv is Python 3.9, past end of life, and
every package added to it is a future upgrade problem.
"""
import hashlib
import os
import urllib.parse
from datetime import datetime, timedelta

TZ = "America/Vancouver"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"

# Full calendar access. Two-way sync means reading events Vesta did not create, which
# the narrower calendar.app.created scope forbids. This is a "sensitive" scope, which is
# why the consent screen shows an unverified-app warning until the project is verified.
SCOPE = "https://www.googleapis.com/auth/calendar"

CALENDAR_NAME = "Vesta"

# Finished work keeps its event and gains this, rather than being deleted, so the
# calendar stays a record of the term and not only a list of what is left.
DONE_MARK = "✓"


class GoogleError(Exception):
    """A failure worth showing the student, with Google's own words where we have them."""

    def __init__(self, message, status=None, resync=False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.resync = resync          # a 410: the sync token is dead, start over


def env(name):
    """An environment variable, with surrounding whitespace removed.

    Credentials get pasted into dashboard fields by hand, and a trailing space rides
    along more often than anyone expects. A client id with a stray space is sent to
    Google as `...googleusercontent.com%20`, which is not a client that exists, and the
    error it produces ("invalid_client: The OAuth client was not found") points at the
    wrong thing entirely.
    """
    return (os.environ.get(name) or "").strip()


def configured():
    return bool(env("GOOGLE_CLIENT_ID") and env("GOOGLE_CLIENT_SECRET"))


# ---------------------------------------------------------------------------
# Pure: Vesta <-> Google event bodies
# ---------------------------------------------------------------------------
def all_day_end(date_iso):
    """Google's all-day end is exclusive: a one-day event on the 15th ends on the 16th.

    Writing the same date for both makes a zero-length event, which is dropped or shown
    on the wrong day depending on the reader.
    """
    d = datetime.strptime(date_iso, "%Y-%m-%d") + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _when(date_iso, time_hhmm, minutes=30, tz=TZ):
    """The start/end pair for one event, all-day or timed."""
    if not time_hhmm:
        return {"date": date_iso}, {"date": all_day_end(date_iso)}
    start = datetime.strptime(f"{date_iso} {time_hhmm}", "%Y-%m-%d %H:%M")
    end = start + timedelta(minutes=minutes)
    return ({"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz},
            {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz})


def patch_body(body):
    """An event body made safe to send as a PATCH.

    PATCH merges into the event Google already has. An assignment with only a due date
    is an all-day event, whose start is `{"date": ...}`; give it a time and Vesta sends
    `{"dateTime": ..., "timeZone": ...}`, the merge keeps the old `date` beside it, and
    Google refuses the result with "Invalid start time." It surfaced when the Canvas
    review filled due times into assignments a syllabus had given only dates, and would
    equally have followed typing a time into one by hand, or clearing it. So the form
    not in use is sent explicitly as null, which is how a PATCH removes a field.
    """
    out = dict(body)
    for key in ("start", "end"):
        part = dict(out.get(key) or {})
        if "dateTime" in part:
            part["date"] = None
        elif "date" in part:
            part["dateTime"] = None
            part["timeZone"] = None
        out[key] = part
    return out


def should_push(item):
    """Deadlines and exams only.

    Class meetings are deliberately not pushed. Five courses of recurring lectures is a
    wall of repeating blocks in a calendar that usually already has them, and the value
    of Google here is answering "what is due", not redrawing a timetable. `meeting_body`
    stays for the day that preference changes.
    """
    return bool(item.get("dueDate"))


def item_body(item, cls=None, tz=TZ):
    """An assignment, quiz or exam as a Google event.

    Finished work keeps its event and gains a tick rather than disappearing, so the
    calendar stays an honest record of the term instead of only showing what is left.
    """
    code = (cls or {}).get("code") or (cls or {}).get("name") or ""
    title = item.get("title") or "Untitled"
    start, end = _when(item["dueDate"], item.get("dueTime"), 30, tz)
    summary = f"{code}: {title}".strip(": ").strip()
    if (item.get("status") or "") == "done":
        summary = f"{DONE_MARK} {summary}"
    body = {
        "summary": summary,
        "start": start,
        "end": end,
        "status": "confirmed",
    }
    bits = []
    if item.get("type"):
        bits.append(item["type"].title())
    if item.get("notes"):
        bits.append(item["notes"])
    bits.append("Added by Vesta")
    body["description"] = "\n\n".join(bits)
    if item.get("location"):
        body["location"] = item["location"]
    return body


def meeting_body(meeting, cls, term, tz=TZ):
    """A weekly class meeting, as one recurring event bounded by the term.

    `recurrence` carries RRULE lines only. Google rejects DTSTART and DTEND inside that
    field: the start is the event's own `start`, and the rule only says how it repeats.
    """
    first = first_occurrence(term.get("startDate"), meeting["day"])
    if not first or not meeting.get("start") or not meeting.get("end"):
        return None
    code = (cls or {}).get("code") or (cls or {}).get("name") or "Class"
    sh, sm = meeting["start"].split(":")
    eh, em = meeting["end"].split(":")
    body = {
        "summary": f"{code} {(meeting.get('kind') or 'lecture').lower()}".strip(),
        "start": {"dateTime": f"{first}T{int(sh):02d}:{sm}:00", "timeZone": tz},
        "end": {"dateTime": f"{first}T{int(eh):02d}:{em}:00", "timeZone": tz},
        "status": "confirmed",
        "description": "Added by Vesta",
    }
    if meeting.get("location"):
        body["location"] = meeting["location"]
    if term.get("endDate"):
        until = datetime.strptime(term["endDate"], "%Y-%m-%d").strftime("%Y%m%dT235959Z")
        body["recurrence"] = [f"RRULE:FREQ=WEEKLY;UNTIL={until}"]
    return body


def first_occurrence(term_start, day):
    """The first date on or after the term start that falls on `day` (0=Sun).

    SFU's own startDate is the term's first day, not the first meeting: REM 388 meets
    Mondays but starts Wednesday Sep 9. Rolling forward is what makes the two agree.
    """
    if not term_start or day is None:
        return None
    d = datetime.strptime(term_start, "%Y-%m-%d")
    for _ in range(7):
        if (d.weekday() + 1) % 7 == day:      # python Mon=0 -> js Sun=0
            return d.strftime("%Y-%m-%d")
        d += timedelta(days=1)
    return None


def _local(raw, tz=TZ):
    """RFC3339 timestamp -> ('YYYY-MM-DD', 'HH:MM') in the student's own zone.

    Google usually returns a dateTime carrying the calendar's offset, but an event
    written by another client comes back as UTC with a trailing Z. Slicing the string
    would read 2026-10-16T03:59:00Z as 03:59 local and put the deadline on the wrong
    day, so the value is parsed and converted rather than trusted as wall time.

    Returns None when the timestamp cannot be read at all. Slicing the raw string as a
    fallback produced dates like "not-a-time", and a corrupt row is worse than a skipped
    event: the caller drops it instead.
    """
    txt = (raw or "").strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"        # 3.9's fromisoformat rejects a bare Z
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo(tz))
        except Exception:
            pass                          # no tz database: wall time is the best we have
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def _stamp(d, tz=TZ):
    """One comparable form for a start/end, whatever shape it arrived in.

    This one keeps the raw string when it cannot be parsed: it is a fingerprint, never
    stored, so an unreadable value still needs to compare equal to itself.
    """
    if not d:
        return ""
    if d.get("date"):
        return d["date"]
    if d.get("dateTime"):
        got = _local(d["dateTime"], tz)
        return "T".join(got) if got else str(d["dateTime"])
    return ""


def from_google(ev, tz=TZ):
    """A Google event as the fields Vesta stores. Returns None for one it cannot place."""
    start = ev.get("start") or {}
    if start.get("date"):
        date_s, time_s, all_day = start["date"], None, True
    elif start.get("dateTime"):
        got = _local(start["dateTime"], tz)
        if got is None:
            return None          # a timestamp Vesta cannot read is not an event it can place
        date_s, time_s = got
        all_day = False
    else:
        return None
    # The finish time, when there is one worth keeping. An all-day event's end is
    # exclusive in Google and meaningless as a clock time, so it is dropped rather than
    # stored as midnight of the following day.
    end_s = None
    if not all_day:
        got_end = _local((ev.get("end") or {}).get("dateTime") or "", tz)
        if got_end and got_end[0] == date_s:
            end_s = got_end[1]
    return {
        "externalId": ev.get("id"),
        "title": (ev.get("summary") or "(no title)").strip(),
        "date": date_s,
        "start": time_s,
        "end": end_s,
        "allDay": all_day,
        "location": ev.get("location") or "",
        "notes": ev.get("description") or "",
        "cancelled": ev.get("status") == "cancelled",
        "etag": ev.get("etag") or "",
        "updated": ev.get("updated") or "",
        "hash": remote_hash(ev),
    }


def remote_hash(ev):
    """Fingerprint of the parts a student would notice changing.

    `etag` moves on every write, including ones Vesta itself made, so it cannot answer
    "did the other side really change?". This can.
    """
    # Timestamps are normalised first. Google can return the same instant as "...Z" on
    # one sync and with the calendar's offset on the next; hashing the raw strings would
    # show a change where nothing changed and report a conflict on an untouched event.
    # It also puts a body Vesta is about to send and one Google sent back on the same
    # footing, which is what makes local_hash and remote_hash comparable at all.
    parts = [ev.get("summary") or "", _stamp(ev.get("start")), _stamp(ev.get("end")),
             ev.get("location") or "", (ev.get("description") or "")[:500],
             ev.get("status") or ""]
    return hashlib.sha256("␟".join(parts).encode("utf-8")).hexdigest()[:32]


def local_hash(body):
    """The same fingerprint over a body Vesta is about to send, so the two compare."""
    return remote_hash(body)


def resolve(link, local_now, remote_now):
    """Who wins, given what each side looked like when they last agreed.

    Vesta is the source of truth, so a genuine two-sided change keeps the local version
    and is flagged rather than silently overwritten either way.
    """
    local_moved = local_now != (link or {}).get("local_hash")
    remote_moved = remote_now != (link or {}).get("remote_hash")
    if local_moved and remote_moved:
        return "conflict"
    if remote_moved:
        return "remote_ahead"
    if local_moved:
        return "push"
    return "clean"


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------
def auth_url(redirect_uri, state):
    """Where to send the student to say yes.

    access_type=offline is what makes Google return a refresh token at all, and
    prompt=consent forces a fresh one even if they have approved this app before.
    """
    q = {"client_id": env("GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri,
         "response_type": "code", "scope": SCOPE, "access_type": "offline",
         "prompt": "consent", "include_granted_scopes": "true", "state": state}
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def _post_token(data):
    import httpx
    try:
        r = httpx.post(TOKEN_URL, data=data, timeout=20)
    except Exception as e:
        raise GoogleError(f"Could not reach Google: {e}")
    if r.status_code >= 400:
        detail = ""
        try:
            j = r.json()
            detail = j.get("error_description") or j.get("error") or ""
        except Exception:
            detail = r.text[:200]
        raise GoogleError(f"Google refused the sign-in: {detail}", r.status_code)
    return r.json()


def exchange_code(code, redirect_uri):
    return _post_token({"code": code, "client_id": env("GOOGLE_CLIENT_ID"),
                        "client_secret": env("GOOGLE_CLIENT_SECRET"),
                        "redirect_uri": redirect_uri, "grant_type": "authorization_code"})


def refresh_token(refresh):
    return _post_token({"refresh_token": refresh, "client_id": env("GOOGLE_CLIENT_ID"),
                        "client_secret": env("GOOGLE_CLIENT_SECRET"),
                        "grant_type": "refresh_token"})


class Client:
    """The thin part. Everything with judgement in it lives above."""

    def __init__(self, access_token):
        self.token = access_token

    def _call(self, method, path, **kw):
        import httpx
        try:
            r = httpx.request(method, API + path,
                              headers={"Authorization": f"Bearer {self.token}"}, timeout=25, **kw)
        except Exception as e:
            raise GoogleError(f"Could not reach Google Calendar: {e}")
        if r.status_code == 410:
            # the sync token is dead: the caller must wipe and start a full sync
            raise GoogleError("Google's sync token expired, so Vesta needs a full resync.",
                              410, resync=True)
        if r.status_code >= 400:
            detail = ""
            try:
                detail = ((r.json().get("error") or {}).get("message")) or ""
            except Exception:
                detail = r.text[:200]
            raise GoogleError(f"Google Calendar said no: {detail}", r.status_code)
        return r.json() if r.content else {}

    def ensure_calendar(self, name=CALENDAR_NAME):
        """Vesta writes to its own calendar, so it is never mixed in with everything else
        and can be hidden or deleted in one action."""
        for c in (self._call("GET", "/users/me/calendarList").get("items") or []):
            if (c.get("summary") or "").strip().lower() == name.lower():
                return c["id"]
        made = self._call("POST", "/calendars", json={"summary": name, "timeZone": TZ})
        return made["id"]

    def list_calendars(self):
        """Every calendar this account can see, so the student can choose.

        `accessRole` decides whether Vesta may write there: a subscribed timetable or a
        holiday calendar is readable but not writable, and trying to push to one fails
        in a way that is hard to explain after the fact.
        """
        out = []
        for c in (self._call("GET", "/users/me/calendarList").get("items") or []):
            role = c.get("accessRole") or "reader"
            out.append({
                "calendarId": c.get("id"),
                "name": c.get("summaryOverride") or c.get("summary") or c.get("id"),
                "primary": bool(c.get("primary")),
                "writable": role in ("owner", "writer"),
                "isVesta": (c.get("summary") or "").strip().lower() == CALENDAR_NAME.lower(),
                "colour": c.get("backgroundColor"),
            })
        return out

    def watch(self, calendar_id, channel_id, address, token=None, ttl_seconds=None):
        """Ask Google to call us when this calendar changes.

        Notifications are headers only: the body is empty, and X-Goog-Resource-State
        says only that something moved. The handler's one sane response is to run the
        same incremental sync the Sync button runs.

        Channels last about a week at most and Google never renews them, so whatever
        registers one has to be able to notice it is close to expiring.
        """
        body = {"id": channel_id, "type": "web_hook", "address": address}
        if token:
            body["token"] = token
        if ttl_seconds:
            body["params"] = {"ttl": str(int(ttl_seconds))}
        return self._call("POST", f"/calendars/{urllib.parse.quote(calendar_id)}/events/watch",
                          json=body)

    def stop_channel(self, channel_id, resource_id):
        """Close a channel. Both ids are needed: storing only one leaves it undeletable."""
        return self._call("POST", "/channels/stop",
                          json={"id": channel_id, "resourceId": resource_id})

    def list_events(self, calendar_id, sync_token=None, page_token=None, time_min=None):
        params = {"maxResults": 250, "showDeleted": bool(sync_token)}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["singleEvents"] = True
            if time_min:
                params["timeMin"] = time_min
        if page_token:
            params["pageToken"] = page_token
        return self._call("GET", f"/calendars/{urllib.parse.quote(calendar_id)}/events", params=params)

    def insert(self, calendar_id, body):
        return self._call("POST", f"/calendars/{urllib.parse.quote(calendar_id)}/events", json=body)

    def patch(self, calendar_id, event_id, body):
        return self._call("PATCH",
                          f"/calendars/{urllib.parse.quote(calendar_id)}/events/{urllib.parse.quote(event_id)}",
                          json=body)

    def delete(self, calendar_id, event_id):
        return self._call("DELETE",
                          f"/calendars/{urllib.parse.quote(calendar_id)}/events/{urllib.parse.quote(event_id)}")

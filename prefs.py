"""Per-account preferences.

Vesta's settings used to live in `localStorage`, which was right when it was one person
on one laptop and wrong the moment accounts arrived: the theme, the focus timer and the
calendar view were per *browser*, so signing in on a phone gave you someone's defaults
rather than your own, and clearing site data wiped them.

They live in `app_settings` now, under one JSON blob keyed `prefs`. That table is
already keyed per user and already covered by row level security, so this needs no
schema change and no new policy: each account reads and writes only its own row.

Everything is merged over `DEFAULTS` on the way out, so a preference added in a later
version appears for accounts that have never written one, and clamped on the way in,
so a hand-written PUT cannot store a focus session of a million minutes and break the
timer for good.
"""
import json

from flask import Blueprint, Response, jsonify, request

import db

bp = Blueprint("prefs", __name__)

PREFS_KEY = "prefs"

DEFAULT_GRADE_SCALE = [
    {"min": 97, "letter": "A+"}, {"min": 93, "letter": "A"}, {"min": 90, "letter": "A-"},
    {"min": 87, "letter": "B+"}, {"min": 83, "letter": "B"}, {"min": 80, "letter": "B-"},
    {"min": 77, "letter": "C+"}, {"min": 73, "letter": "C"}, {"min": 70, "letter": "C-"},
    {"min": 67, "letter": "D+"}, {"min": 63, "letter": "D"}, {"min": 60, "letter": "D-"},
    {"min": 0, "letter": "F"},
]

FOCUS_DEFAULTS = {
    "work": 25, "shortBreak": 5, "longBreak": 15, "sessionsBeforeLong": 4,
    "autoStartBreaks": False, "autoStartFocus": False,
    "alarmOn": True, "alarmSound": "chime",
    "scene": "none", "customScene": "", "accent": "#005FFB", "ambient": "off",
}

DEFAULTS = {
    # Appearance
    "theme": "light",
    "sidebarCollapsed": False,
    # Grades and Focus can come off the sidebar to keep it short. The pages stay: they
    # are still reached from search, the dashboard and Start Focus Session.
    "sidebarShowGrades": True,
    "sidebarShowFocus": True,
    # Calendar
    "calendarDefaultView": "month",
    "calendarDetail": "detailed",
    # Notifications. Off by default: a browser permission prompt on first load, for
    # something nobody asked for, is the fastest way to be denied permission forever.
    "notifyFocusDone": False,
    "notifyDueSoon": False,
    "notifyDueSoonHours": 24,
    # Grades
    "defaultGradeScale": DEFAULT_GRADE_SCALE,
    # Notes and editor
    "editorFontSize": "medium",
    "editorSpellcheck": True,
    # Files
    "filesDefaultLayout": "grid",
    # Focus
    "focus": FOCUS_DEFAULTS,
    # Headstart
    "headstartView": "cards",
    # The lightning bolt beside Send in a thread: on, replies start at once with little
    # thinking; off, the model takes its time. On by default, because a blank wait is
    # what made threads feel slow.
    "threadFast": True,
    # Full screen for a thread: "auto" is on for a phone and off otherwise, until the
    # student picks for themselves with the button or the shortcut.
    "threadFullscreen": "auto",
}

_CHOICES = {
    "theme": ("light", "dark"),
    "calendarDefaultView": ("month", "week", "list"),
    "calendarDetail": ("simple", "detailed"),
    "editorFontSize": ("small", "medium", "large"),
    "filesDefaultLayout": ("grid", "list"),
    "headstartView": ("cards", "list"),
    "threadFullscreen": ("auto", "on", "off"),
}

_FOCUS_RANGES = {
    "work": (5, 120), "shortBreak": (1, 30), "longBreak": (5, 60),
    "sessionsBeforeLong": (2, 8),
}


def _num(value, low, high, fallback):
    try:
        return min(high, max(low, int(float(value))))
    except (TypeError, ValueError):
        return fallback


def _clean_scale(rows):
    """A grading scale is only useful sorted and sane, so store it that way."""
    if not isinstance(rows, list) or not rows:
        return list(DEFAULT_GRADE_SCALE)
    out = []
    for r in rows[:30]:
        if not isinstance(r, dict):
            continue
        letter = str(r.get("letter") or "").strip()[:4]
        if not letter:
            continue
        out.append({"min": _num(r.get("min"), 0, 100, 0), "letter": letter})
    if not out:
        return list(DEFAULT_GRADE_SCALE)
    out.sort(key=lambda r: r["min"], reverse=True)
    return out


def clean(patch, base=None):
    """Merge a patch over `base`, keeping only known keys and legal values."""
    out = dict(base or DEFAULTS)
    if not isinstance(patch, dict):
        return out
    for key, value in patch.items():
        if key not in DEFAULTS:
            continue
        if key in _CHOICES:
            if value in _CHOICES[key]:
                out[key] = value
        elif key == "notifyDueSoonHours":
            out[key] = _num(value, 1, 168, DEFAULTS[key])
        elif key == "defaultGradeScale":
            out[key] = _clean_scale(value)
        elif key == "focus":
            focus = dict(out.get("focus") or FOCUS_DEFAULTS)
            if isinstance(value, dict):
                for fk, fv in value.items():
                    if fk not in FOCUS_DEFAULTS:
                        continue
                    if fk in _FOCUS_RANGES:
                        low, high = _FOCUS_RANGES[fk]
                        focus[fk] = _num(fv, low, high, FOCUS_DEFAULTS[fk])
                    elif isinstance(FOCUS_DEFAULTS[fk], bool):
                        focus[fk] = bool(fv)
                    elif fk == "customScene":
                        # An image address, and real ones (a CDN link with a query
                        # string) run well past 120 characters. Cutting one short
                        # broke the image after the next reload.
                        focus[fk] = str(fv)[:2048]
                    else:
                        focus[fk] = str(fv)[:120]
            out["focus"] = focus
        elif isinstance(DEFAULTS[key], bool):
            out[key] = bool(value)
    return out


def read(conn):
    raw = db.get_setting(conn, PREFS_KEY, "")
    stored = {}
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = {}
    # Merged over the defaults, so a preference introduced later simply appears.
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in stored.items() if k in DEFAULTS})
    merged["focus"] = dict(FOCUS_DEFAULTS, **(stored.get("focus") or {}))
    return clean(merged, DEFAULTS)


# Everything an account owns, in the order a person would want to read it. Kept as an
# explicit list rather than reading the catalogue, so a table added later is an omission
# someone notices rather than a silent change to what "all my data" means.
EXPORT_TABLES = [
    "semesters", "term_settings", "classes", "schedule_entries", "grade_categories",
    "items", "subtasks", "item_files", "materials", "note_folders", "notes",
    "note_links", "note_versions", "events", "calendar_accounts", "calendar_imports",
    "sync_links", "syllabus_imports", "syllabus_topics", "headstarts",
    "headstart_sources", "rubrics", "quizzes", "quiz_questions", "quiz_attempts",
    "flashcard_decks", "flashcards", "app_settings", "ai_usage",
    "threads", "thread_messages", "thread_sources", "humanizer_runs", "week_marks", "week_plan_items",
]


@bp.route("/api/export")
def export_everything():
    """One JSON file of everything this account owns.

    No user filtering appears in these queries and none is needed: on Postgres row
    level security has already narrowed every table to the caller, and on SQLite there
    is only ever one person. A table that does not exist is skipped rather than fatal,
    because the two databases have drifted before.
    """
    conn = db.get_db()
    out = {"exportedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
           "tables": {}}
    try:
        for table in EXPORT_TABLES:
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            except Exception:
                continue
            out["tables"][table] = [dict(r) for r in rows]
    finally:
        conn.close()
    body = json.dumps(out, indent=2, default=str)
    return Response(
        body, mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="vesta-export.json"'})


@bp.route("/api/prefs", methods=["GET", "PUT"])
def prefs():
    conn = db.get_db()
    try:
        if request.method == "PUT":
            patch = request.get_json(force=True) or {}
            merged = clean(patch, read(conn))
            db.set_setting(conn, PREFS_KEY, json.dumps(merged))
            return jsonify(merged)
        return jsonify(read(conn))
    finally:
        conn.close()

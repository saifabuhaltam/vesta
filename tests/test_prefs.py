"""Preferences: defaults appear, patches merge, and nonsense is clamped rather than stored."""
import pytest

import prefs
import app as vesta_app
from test_auth import FakeSupabase, supa, client, sign_in     # noqa: F401


@pytest.fixture(autouse=True)
def clean_prefs():
    """One SQLite file serves every test here, so clear the row between them."""
    import db
    yield
    conn = db.get_db()
    conn.execute("DELETE FROM app_settings WHERE key=?", (prefs.PREFS_KEY,))
    conn.commit()
    conn.close()


@pytest.fixture
def signed_in(client, supa):
    sign_in(client, supa)
    return client


def test_prefs_need_a_session(client):
    assert client.get("/api/prefs").status_code == 401


def test_defaults_come_back_for_a_new_account(signed_in):
    body = signed_in.get("/api/prefs").get_json()
    assert body["theme"] == "light"
    assert body["calendarDefaultView"] == "month"
    assert body["focus"]["work"] == 25
    assert body["defaultGradeScale"][0]["letter"] == "A+"


def test_a_patch_merges_and_persists(signed_in):
    signed_in.put("/api/prefs", json={"theme": "dark"})
    body = signed_in.get("/api/prefs").get_json()
    assert body["theme"] == "dark"
    assert body["calendarDefaultView"] == "month"      # untouched


def test_a_focus_patch_keeps_the_other_focus_fields(signed_in):
    signed_in.put("/api/prefs", json={"focus": {"work": 50}})
    focus = signed_in.get("/api/prefs").get_json()["focus"]
    assert focus["work"] == 50
    assert focus["shortBreak"] == 5                    # not wiped by the partial patch


def test_an_illegal_choice_is_ignored(signed_in):
    signed_in.put("/api/prefs", json={"theme": "chartreuse"})
    assert signed_in.get("/api/prefs").get_json()["theme"] == "light"


def test_focus_durations_are_clamped(signed_in):
    body = signed_in.put("/api/prefs", json={"focus": {"work": 99999, "shortBreak": -4}}).get_json()
    assert body["focus"]["work"] == 120
    assert body["focus"]["shortBreak"] == 1


def test_unknown_keys_are_dropped(signed_in):
    body = signed_in.put("/api/prefs", json={"rm": "-rf", "theme": "dark"}).get_json()
    assert "rm" not in body
    assert body["theme"] == "dark"


def test_due_soon_hours_are_bounded(signed_in):
    assert signed_in.put("/api/prefs", json={"notifyDueSoonHours": 100000}).get_json()["notifyDueSoonHours"] == 168
    assert signed_in.put("/api/prefs", json={"notifyDueSoonHours": 0}).get_json()["notifyDueSoonHours"] == 1


def test_a_grade_scale_is_sorted_and_cleaned(signed_in):
    body = signed_in.put("/api/prefs", json={"defaultGradeScale": [
        {"min": 50, "letter": "C"},
        {"min": 90, "letter": "A"},
        {"min": 200, "letter": "Z"},
        {"min": 70, "letter": ""},          # no letter: dropped
        "junk",                              # not an object: dropped
    ]}).get_json()
    scale = body["defaultGradeScale"]
    assert [r["letter"] for r in scale] == ["Z", "A", "C"]
    assert scale[0]["min"] == 100            # clamped from 200
    assert all(isinstance(r, dict) for r in scale)


def test_an_empty_grade_scale_falls_back_to_the_default(signed_in):
    body = signed_in.put("/api/prefs", json={"defaultGradeScale": []}).get_json()
    assert body["defaultGradeScale"] == prefs.DEFAULT_GRADE_SCALE


def test_booleans_are_coerced(signed_in):
    body = signed_in.put("/api/prefs", json={"editorSpellcheck": 0, "notifyFocusDone": 1}).get_json()
    assert body["editorSpellcheck"] is False
    assert body["notifyFocusDone"] is True


def test_corrupt_stored_json_falls_back_to_defaults(signed_in):
    import db
    conn = db.get_db()
    db.set_setting(conn, prefs.PREFS_KEY, "{not json at all")
    conn.close()
    assert signed_in.get("/api/prefs").get_json()["theme"] == "light"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def test_export_needs_a_session(client):
    assert client.get("/api/export").status_code == 401


def test_export_returns_a_downloadable_json_file(signed_in):
    r = signed_in.get("/api/export")
    assert r.status_code == 200
    assert r.mimetype == "application/json"
    assert "attachment" in r.headers["Content-Disposition"]
    assert "vesta-export.json" in r.headers["Content-Disposition"]


def test_export_contains_every_listed_table(signed_in):
    import prefs as p
    body = signed_in.get("/api/export").get_json()
    assert "exportedAt" in body
    missing = [t for t in p.EXPORT_TABLES if t not in body["tables"]]
    assert missing == [], f"tables absent from the export: {missing}"


def test_export_carries_the_rows_it_should(signed_in):
    """A preference written now must appear in the file a moment later."""
    signed_in.put("/api/prefs", json={"theme": "dark"})
    body = signed_in.get("/api/export").get_json()
    keys = [r["key"] for r in body["tables"]["app_settings"]]
    assert "prefs" in keys

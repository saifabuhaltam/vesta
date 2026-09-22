"""The SQLite/Postgres traps that only bite in production.

Every test here runs against SQLite, which is the point: each of these bugs passed
locally and returned a 500 on the deployed Postgres database. They are cheap guards
against the whole class of mistake coming back, since no Postgres is needed to run them.
"""
import re

import pytest

import app as vesta_app
import db
from test_auth import FakeSupabase, supa, client, sign_in     # noqa: F401


@pytest.fixture
def signed_in(client, supa):
    sign_in(client, supa)
    return client


@pytest.fixture
def a_class(signed_in):
    r = signed_in.post("/api/classes", json={"name": "Psychology 300W", "code": "PSYC 300W"})
    assert r.status_code == 201
    return r.get_json()["id"]


# ---------------------------------------------------------------------------
# NUL bytes in extracted text
# ---------------------------------------------------------------------------
def test_db_safe_text_drops_the_bytes_postgres_refuses():
    out = vesta_app.db_safe_text("start\x00nul\x0bvt\ttab\nline\r\n")
    assert "\x00" not in out
    assert "\x0b" not in out
    assert out == "startnulvt\ttab\nline\r\n"


def test_db_safe_text_leaves_ordinary_text_alone():
    assert vesta_app.db_safe_text("Readings, week 6 — Tulving (1972)") == \
        "Readings, week 6 — Tulving (1972)"
    assert vesta_app.db_safe_text("") == ""
    assert vesta_app.db_safe_text(None) is None


def test_an_uploaded_file_never_stores_a_nul(signed_in, a_class):
    """A PDF whose extracted text holds a NUL used to 500, and only that one file did."""
    from io import BytesIO
    body = b"clean\x00dirty\x0bmore\nend"
    r = signed_in.post(f"/api/classes/{a_class}/materials",
                       data={"file": (BytesIO(body), "reading.txt")},
                       content_type="multipart/form-data")
    assert r.status_code == 201
    conn = db.get_db()
    text = conn.execute("SELECT extracted_text FROM materials WHERE id=?",
                        (r.get_json()["id"],)).fetchone()["extracted_text"]
    conn.close()
    assert "\x00" not in text and "\x0b" not in text
    assert "clean" in text and "end" in text


# ---------------------------------------------------------------------------
# One assignment per column in an UPDATE
# ---------------------------------------------------------------------------
def test_a_drag_onto_a_folder_sets_folder_id_once(signed_in, a_class, monkeypatch):
    """Dragging sends classId and folderId together.

    Listing `folder_id` twice in one SET clause is last-one-wins in SQLite and
    "multiple assignments to same column" in Postgres.
    """
    from io import BytesIO
    fid = signed_in.post(f"/api/classes/{a_class}/file-folders",
                         json={"name": "Readings"}).get_json()["id"]
    mid = signed_in.post(f"/api/classes/{a_class}/materials",
                         data={"file": (BytesIO(b"hello"), "reading.txt")},
                         content_type="multipart/form-data").get_json()["id"]

    seen = []

    class Recording:
        """Passes everything through, keeping the SQL the route builds."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            seen.append(sql)
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    real_get_db = vesta_app.get_db
    monkeypatch.setattr(vesta_app, "get_db", lambda *a, **k: Recording(real_get_db(*a, **k)))
    r = signed_in.put(f"/api/materials/{mid}", json={"classId": a_class, "folderId": fid})
    assert r.status_code == 200
    assert r.get_json()["folderId"] == fid

    updates = [q for q in seen if q.lstrip().upper().startswith("UPDATE MATERIALS")]
    assert updates, "the route did not update the row at all"
    for sql in updates:
        assigned = re.findall(r"(\w+)\s*=\s*\?", sql.split("WHERE")[0])
        assert len(assigned) == len(set(assigned)), f"column assigned twice: {sql}"


def test_a_drag_to_the_inbox_still_clears_the_folder(signed_in, a_class):
    from io import BytesIO
    fid = signed_in.post(f"/api/classes/{a_class}/file-folders",
                         json={"name": "Readings"}).get_json()["id"]
    mid = signed_in.post(f"/api/classes/{a_class}/materials",
                         data={"file": (BytesIO(b"hello"), "reading.txt")},
                         content_type="multipart/form-data").get_json()["id"]
    signed_in.put(f"/api/materials/{mid}", json={"classId": a_class, "folderId": fid})
    out = signed_in.put(f"/api/materials/{mid}", json={"classId": None, "folderId": None})
    assert out.status_code == 200
    assert out.get_json()["folderId"] is None


# ---------------------------------------------------------------------------
# `IS ?` is SQLite-only
# ---------------------------------------------------------------------------
def test_no_query_compares_with_is_placeholder():
    """`col IS ?` is SQLite's null-safe equality and a Postgres syntax error."""
    import pathlib
    root = pathlib.Path(vesta_app.__file__).resolve().parent
    offenders = []
    for name in ("app.py", "db.py", "links.py", "threads.py", "prefs.py", "auth.py"):
        src = (root / name).read_text()
        for n, line in enumerate(src.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"\bIS\s+\?", line):
                offenders.append(f"{name}:{n}: {line.strip()}")
    assert not offenders, "use `IS NULL` or `= ?`, branching on the value:\n" + "\n".join(offenders)

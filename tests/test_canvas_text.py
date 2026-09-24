"""Every Canvas file's text is read, whatever its size, without keeping the big ones.

Headstart's source picker showed most of IAT 201 as "no readable text": every file over
the 5 MB automatic-download line, lecture decks and readings of 8 to 113 MB, although
every one had text. These tests hold the fix to its promises: the text arrives, the
file itself is not kept, video is left alone, nothing is read twice for nothing, and a
file he opened or an apply he undid is not written over.
"""
import os
import uuid

import pytest

import canvas
import canvas_sync as sync
import db
from test_canvas_review import COURSE, conn, class_id, connect    # noqa: F401


def link(conn, class_id, filename, size=20 * 1024 * 1024, mimetype=None, kind="link",
         text=None, key=None):
    mid = str(uuid.uuid4())
    conn.execute("INSERT INTO materials (id, semester_id, class_id, title, kind, url, filename,"
                 " size, mimetype, extracted_text, import_key, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (mid, db.active_semester_id(conn), class_id, filename, kind,
                  "https://canvas.sfu.ca/files/%s/download" % mid[:6], filename, size, mimetype,
                  text, key or "canvas:file:" + mid[:8], "2026-09-01"))
    conn.commit()
    return mid


def row(conn, mid):
    return conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()


@pytest.fixture
def downloads(monkeypatch):
    """Canvas, stubbed: every download is a small text file, and each one is counted."""
    seen = []

    def fake(self, url, dest, max_bytes=None):
        seen.append((url, dest))
        with open(dest, "w") as fh:
            fh.write("Lecture 3: attention and perception.")
        return 36

    monkeypatch.setattr(canvas.Client, "download", fake)
    return seen


@pytest.fixture
def reader(monkeypatch):
    import app as vesta_app
    monkeypatch.setattr(sync, "READ_TEXT",
                        lambda mid, user_id=None: vesta_app.read_canvas_text(mid, user_id=user_id))
    return vesta_app


def test_a_big_file_s_text_is_read_and_the_file_is_not_kept(conn, class_id, downloads, reader):
    connect(conn, class_id)
    mid = link(conn, class_id, "IAT201.3 Lecture.txt")
    assert reader.read_canvas_text(mid) is True
    r = row(conn, mid)
    assert r["extracted_text"] == "Lecture 3: attention and perception."
    assert (r["kind"], r["stored_name"]) == ("link", None)       # still opens by fetching
    dest = downloads[0][1]
    assert not os.path.exists(os.path.dirname(dest))              # the temporary copy is gone


def test_what_the_page_shows_turns_from_no_text_to_readable(conn, class_id, downloads, reader):
    connect(conn, class_id)
    mid = link(conn, class_id, "Reading.txt")
    before = reader.serialize_material(row(conn, mid))
    reader.read_canvas_text(mid)
    after = reader.serialize_material(row(conn, mid))
    assert (before["hasText"], after["hasText"]) == (False, True)


def test_every_missing_text_is_read_and_video_is_left_alone(conn, class_id, downloads, reader):
    connect(conn, class_id)
    a = link(conn, class_id, "Deck one.txt")
    b = link(conn, class_id, "Reading two.txt")
    v = link(conn, class_id, "Lecture recording.mp4", mimetype="video/mp4")
    report = sync.read_missing_text(None)
    assert report["read"] == 2
    assert row(conn, a)["extracted_text"] and row(conn, b)["extracted_text"]
    assert row(conn, v)["extracted_text"] is None
    assert not any("recording" in d for _, d in downloads)


def test_a_file_already_tried_is_not_downloaded_again(conn, class_id, reader, monkeypatch):
    """A scanned PDF has no text to find; re-downloading it on every check would cost
    minutes each time for nothing."""
    connect(conn, class_id)
    calls = []

    def empty(self, url, dest, max_bytes=None):
        calls.append(url)
        open(dest, "w").close()
        return 0

    monkeypatch.setattr(canvas.Client, "download", empty)
    link(conn, class_id, "Scanned book.txt")
    sync.read_missing_text(None)
    sync.read_missing_text(None)
    assert len(calls) == 1


def test_a_file_whose_size_changed_on_canvas_is_tried_again(conn, class_id, reader, monkeypatch):
    connect(conn, class_id)
    calls = []

    def empty(self, url, dest, max_bytes=None):
        calls.append(url)
        open(dest, "w").close()
        return 0

    monkeypatch.setattr(canvas.Client, "download", empty)
    mid = link(conn, class_id, "Scanned book.txt")
    sync.read_missing_text(None)
    conn.execute("UPDATE materials SET size=size+1 WHERE id=?", (mid,))
    conn.commit()
    sync.read_missing_text(None)
    assert len(calls) == 2


def test_a_file_he_opened_is_not_read_again_or_written_over(conn, class_id, downloads, reader):
    connect(conn, class_id)
    mid = link(conn, class_id, "Opened.txt", kind="file", text="the real text")
    assert reader.read_canvas_text(mid) is False
    assert row(conn, mid)["extracted_text"] == "the real text"
    assert downloads == []


def test_a_row_removed_while_its_text_was_read_stays_removed(conn, class_id, reader, monkeypatch):
    """An Undo can land while a big deck is still downloading."""
    connect(conn, class_id)
    mid = link(conn, class_id, "Deck.txt")

    def undone_meanwhile(self, url, dest, max_bytes=None):
        with open(dest, "w") as fh:
            fh.write("text")
        c2 = db.get_db()
        c2.execute("DELETE FROM materials WHERE id=?", (mid,))
        c2.commit()
        c2.close()
        return 4

    monkeypatch.setattr(canvas.Client, "download", undone_meanwhile)
    reader.read_canvas_text(mid)
    assert row(conn, mid) is None


def test_a_check_reads_the_text_it_is_missing(conn, class_id, downloads, reader, monkeypatch):
    connect(conn, class_id)
    mid = link(conn, class_id, "Deck.txt")

    class FakeClient(canvas.Client):
        def __init__(self, host, token):
            super().__init__("canvas.sfu.ca", "tok")

        def profile(self):
            return {}

        def course(self, cid):
            return {"id": cid}

        def assignment_groups(self, cid):
            return []

        def modules(self, cid):
            return []

        def course_files(self, cid, known=None, modules=None):
            return []

    monkeypatch.setattr(canvas, "Client", FakeClient)
    sync.check_account(None)
    assert row(conn, mid)["extracted_text"] == "Lecture 3: attention and perception."


# ---------------------------------------------------------------------------
# what the source pickers are told about each file
# ---------------------------------------------------------------------------

def test_the_picker_learns_each_file_s_folder_size_and_why_it_cannot_be_read(conn, class_id):
    """The picker said "no readable text" about a lecture video and a deck whose text was
    still being read alike, and listed fifty names with nothing to group them by."""
    import app as vesta_app
    import ai
    fid = str(uuid.uuid4())
    conn.execute("INSERT INTO file_folders (id, class_id, parent_id, name, kind, sort_order, created_at)"
                 " VALUES (?,?,?,?,?,?,?)", (fid, class_id, None, "Week 1", "custom", 0, "2026-09-01"))
    conn.commit()
    deck = link(conn, class_id, "Lecture 1.pdf")
    conn.execute("UPDATE materials SET folder_id=? WHERE id=?", (fid, deck))
    link(conn, class_id, "Recording.mp4", mimetype="video/mp4")
    link(conn, class_id, "Photo.png", mimetype="image/png")
    link(conn, class_id, "Readings.zip")
    link(conn, class_id, "Notes.txt", kind="file", text="words")
    conn.commit()
    with vesta_app.app.test_request_context():
        body = ai.ai_context(class_id).get_json()
    by = {f["title"]: f for f in body["files"]}
    assert (by["Lecture 1.pdf"]["folderName"], by["Lecture 1.pdf"]["reason"]) == ("Week 1", "reading")
    assert by["Recording.mp4"]["reason"] == "video"
    assert by["Photo.png"]["reason"] == "image"
    assert by["Readings.zip"]["reason"] == "archive"
    assert (by["Notes.txt"]["readable"], by["Notes.txt"]["reason"]) == (True, None)
    assert by["Lecture 1.pdf"]["size"] == 20 * 1024 * 1024

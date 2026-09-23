"""Canvas sync: what a sync would change, before it changes anything.

`canvas.py` reads Canvas and produces a plan. This module holds the plan up against
what is already in a class and works out, field by field, what is new, what disagrees
and what is untouched. Nothing here writes: `compare` returns a draft for the review
screen, and the apply path consumes only the parts that came back ticked.

The shape is deliberately the one `syllabus_import.compare` already produces, because
Saif already has a screen that renders it and already understands how it behaves. The
rules it encodes are his:

* **nothing Vesta holds is overwritten without a look.** A field Vesta has not got is
  filled silently; a field that disagrees is shown with both values and its tick off.
* **Canvas wins where he has not decided anything**, which is what "Canvas should win
  unless I change the grades by hand" means in practice once you notice that a column
  cannot tell a typed value from a synced one. The tick is what tells them apart.
* **nothing is ever deleted.** An assignment or a file that Vesta has and Canvas does
  not is left alone and not even offered, because Canvas is one source among several
  here: a syllabus import and his own typing are the others.

Two cases the assignment side gets right only because it is asked to:

* **an assignment he already typed, or imported from a syllabus, is not a duplicate.**
  It has no Canvas key, so matching falls back to the title, exactly as the syllabus
  importer falls back. Without that, the first Canvas sync would double every
  assignment in a class that has had a syllabus imported.
* **a due date Canvas does not have is not a due date to remove.** IAT201 carries no
  due dates at all, so a sync that treated absence as an instruction would empty the
  schedule his syllabus import filled in.
"""
import re

import canvas

# What a file's identity is built from when Canvas has not told us it is ours. Two
# files in one class with the same name and the same byte count are the same file.
FILE_KEY_FIELDS = ("filename", "size")


def norm(text):
    """A title reduced to what is worth comparing. Same spelling as `syllabus_import`,
    so the two importers agree about when two assignments are the same one."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def file_import_key(file_id):
    """`materials.import_key` for a Canvas file.

    Distinct from an assignment's `canvas:<id>` because Canvas numbers files and
    assignments in separate sequences, and file 500 and assignment 500 both exist.
    """
    return "canvas:file:" + str(file_id)


def _col(row, name, default=None):
    """One column, whether the row is a sqlite3.Row, a psycopg row or a plain dict.

    `sqlite3.Row` has no `.get`, and a column added by a later migration may be absent
    from a row read on a database that has not run it yet. `app.py` uses this same
    `in row.keys()` idiom for `import_key`.
    """
    try:
        if name in row.keys():
            value = row[name]
            return default if value is None else value
    except AttributeError:
        return row.get(name, default) if hasattr(row, "get") else default
    return default


# ---------------------------------------------------------------------------
# assignments
# ---------------------------------------------------------------------------

# Which fields are compared, and what they are called on the row. `score` is in here
# and `notes` is not: an assignment description is long, is rewritten by instructors
# for reasons that do not matter, and would make every sync look like a change.
ITEM_FIELDS = (
    ("dueDate", "due_date"),
    ("dueTime", "due_time"),
    ("type", "type"),
    ("weight", "weight"),
    ("score", "score"),
)


def compare_items(existing, plan, seen=()):
    """Annotate each planned assignment with what it would do to this class.

    `existing` is the class's `items` rows; `plan` is `canvas.plan_course` output.
    Every planned item comes back carrying `change` (new, changed or same),
    `existingId` where one was matched, a list of `changes`, and `include`, which is
    the tick the review screen starts with.

    `seen` is the set of Canvas keys previous syncs have written into this class, and
    it is how a deletion is remembered without a column or a hook on the delete route.
    A key that was written once and now matches no row means he deleted that assignment
    on purpose, so it comes back marked `dismissed` and is kept off the review screen
    rather than being offered as new for the rest of the term. IAT201's twenty-five
    readings are why: without this, deleting them once would mean skipping past them
    on every sync forever.
    """
    rows = list(existing or [])
    used = set()
    seen = set(seen or ())
    trust_weights = bool(plan.get("weightsFromCanvas"))
    out = []

    for item in plan.get("items") or []:
        match = _match_item(rows, used, item)
        if not match:
            deleted = item.get("importKey") in seen
            item["change"] = "dismissed" if deleted else "new"
            item["existingId"] = None
            item["changes"] = []
            item["include"] = not deleted
            out.append(item)
            continue

        used.add(_col(match, "id"))
        item["existingId"] = _col(match, "id")
        # His name for it, which is what the review should call it: once a duplicate is
        # merged, his "Reading quiz (Week 1)" is linked to Canvas's "Week 1 Readings Quiz".
        item["existingTitle"] = _col(match, "title")
        changes = []
        for field, column in ITEM_FIELDS:
            after = item.get(field)
            if after is None or after == "":
                # Canvas does not know. That is not an instruction to clear what Vesta
                # has, and IAT201, which carries no due dates at all, is why.
                continue
            if field == "weight" and not trust_weights:
                # A points-based course's weights are arithmetic on an incomplete
                # Canvas setup: IAT201's are all 0% and PSYC300W's first is 62.5%. They
                # are not offered at all. Offering them unticked was the first design,
                # and against his real courses it put 32 junk weight changes in front
                # of him on every review, for good. See CANVAS.md.
                continue
            if field == "weight" and _col(match, "category_id"):
                # His row sits in a grade category, which decides its weight. A weight of
                # its own would sit unused beside it, and look like it counted.
                continue
            before = _col(match, column)
            if _same(before, after):
                continue
            fill = before is None or before == ""
            changes.append({"field": field, "before": before, "after": after, "fill": fill})

        # A description is offered only into an empty one. Where he already has text,
        # his stands and nothing is offered: instructors reword descriptions all term,
        # and every rewording would otherwise look like a change to review.
        offered = (item.get("notes") or "").strip()
        mine = _col(match, "notes") or ""
        if offered and not canvas.plain_text(mine).strip():
            changes.append({"field": "notes", "before": mine, "after": offered, "fill": True})
        elif offered and mine.strip() != offered and \
                canvas.plain_words(mine) == canvas.plain_words(offered):
            # The same words in a different layout: a description saved before Vesta
            # kept Canvas's formatting, which arrived as one wall of text. Word for word
            # the same is what makes it safe to call Canvas's copy rather than his.
            changes.append({"field": "notes", "before": mine, "after": offered, "fill": True,
                            "relayout": True})

        item["changes"] = changes
        item["change"] = "changed" if changes else "same"
        # A gap is filled without asking; a disagreement waits to be ticked. An item
        # where every change is a fill is ticked as a whole, which is the common case
        # of a brand-new class syncing for the first time.
        item["include"] = bool(changes) and all(c["fill"] for c in changes)
        out.append(item)

    return out


def _match_item(rows, used, item):
    """The row this planned assignment is, if the class already holds it.

    The Canvas key first, because it is exact and survives a rename on either side.
    Then the title, which is what catches an assignment he typed himself or imported
    from the syllabus before ever connecting Canvas. Without the second pass the first
    sync of a class that has had a syllabus imported would duplicate all of it.
    """
    key = item.get("importKey")
    for row in rows:
        if _col(row, "id") in used:
            continue
        if key and _col(row, "import_key") == key:
            return row
    title = norm(item.get("title"))
    if not title:
        return None
    for row in rows:
        if _col(row, "id") in used:
            continue
        if norm(_col(row, "title")) == title:
            return row
    return None


def _same(before, after):
    """Whether two values are the same for the purpose of showing a change.

    Numbers are compared as numbers, because a weight read back as 20.0 and a weight
    computed as 20 are not a change anyone wants to look at, and a row that came from
    SQLite and one that came from Postgres do not always agree about which it is.
    """
    if before is None or before == "":
        return False
    a, b = canvas._number(before), canvas._number(after)
    if a is not None and b is not None:
        return round(a, 4) == round(b, 4)
    return str(before).strip() == str(after).strip()


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def compare_files(existing, files, folder_for=None, seen=()):
    """Annotate each Canvas file with what it would do to this class's Files tab.

    `existing` is the class's `materials` rows, `files` the Canvas file objects from
    `canvas.Client.course_files`. `folder_for` optionally maps a module name to the
    folder a file should land in.

    Three outcomes, and the third is the one worth getting right:

    * **new** — Vesta has nothing like it. Ticked.
    * **same** — matched by Canvas key, or by name and byte count. Nothing to do.
    * **changed** — the same filename, a different size. Ticked off, and offered as an
      *addition* rather than a replacement. A file already in Vesta may be the copy he
      annotated, and a sync that quietly overwrote it would destroy work that exists
      nowhere else. Replacing is his decision to make, once, on screen.
    """
    rows = list(existing or [])
    used = set()
    seen = set(seen or ())
    out = []
    for f in files or []:
        name = (f.get("display_name") or f.get("filename") or "").strip()
        size = f.get("size")
        row = _match_file(rows, used, f, name)
        entry = {
            "canvasId": f.get("id"),
            "importKey": file_import_key(f.get("id")),
            "title": name,
            "filename": name,
            "size": size,
            "mimetype": f.get("content-type") or f.get("content_type"),
            "url": f.get("url"),
            "module": f.get("_module"),
            "folderName": (folder_for or {}).get(f.get("_module")) if folder_for else f.get("_module"),
            "updatedAt": f.get("modified_at") or f.get("updated_at"),
        }
        if not row:
            # Deleted on purpose after an earlier sync brought it in. Same reasoning as
            # assignments: a file he threw away is not a file to keep offering.
            deleted = entry["importKey"] in seen
            entry.update(change="dismissed" if deleted else "new", existingId=None,
                         include=not deleted, conflict=None)
            out.append(entry)
            continue

        used.add(_col(row, "id"))
        existing_size = _col(row, "size")
        entry["existingId"] = _col(row, "id")
        ours = _col(row, "import_key") == entry["importKey"]
        if ours and not _same_size(existing_size, size):
            # A file this feature brought in, and Canvas's copy has since changed size:
            # the professor replaced it. Unlike a file he uploaded, the copy here came
            # from Canvas and holds nothing of his, so updating it is safe to offer.
            # Only offered, never done, and "keep" is remembered like any other refusal.
            entry.update(change="updated", include=False, conflict={
                "reason": "Canvas has a newer version of this file.",
                "existingTitle": _col(row, "title") or _col(row, "filename"),
                "existingSize": existing_size,
                "canvasSize": size,
                "options": ["update", "keep"],
            })
        elif ours or _same_size(existing_size, size):
            entry.update(change="same", include=False, conflict=None)
        else:
            entry.update(change="changed", include=False, conflict={
                "reason": "A file with this name is already in this class, and Canvas's "
                          "copy is a different size.",
                "existingTitle": _col(row, "title") or _col(row, "filename"),
                "existingSize": existing_size,
                "canvasSize": size,
                # Never 'replace'. The copy already here may be the annotated one.
                "options": ["keep", "addBoth"],
            })
        out.append(entry)
    return out


def _match_file(rows, used, f, name):
    key = file_import_key(f.get("id"))
    for row in rows:
        if _col(row, "id") in used:
            continue
        if _col(row, "import_key") == key:
            return row
    wanted = norm(name)
    if not wanted:
        return None
    for row in rows:
        if _col(row, "id") in used:
            continue
        if norm(_col(row, "filename") or _col(row, "title")) == wanted:
            return row
    return None


def _same_size(before, after):
    """Byte counts, tolerantly. A row stored before `size` was recorded has None, which
    is not evidence of a difference, so it is treated as a match on the name alone."""
    if before in (None, "") or after in (None, ""):
        return True
    a, b = canvas._number(before), canvas._number(after)
    return a is not None and b is not None and int(a) == int(b)


# ---------------------------------------------------------------------------
# the draft
# ---------------------------------------------------------------------------

def compare(existing_items, existing_materials, plan, files, folder_for=None, seen=()):
    """The whole review draft for one course: assignments, files and a count.

    `summary` is what the screen leads with, so that a sync which would change nothing
    can say so in a sentence instead of making him read two lists. Dismissed rows are
    counted but kept out of the two lists, and `dismissed` in the summary is what the
    screen needs to offer "3 you deleted are hidden, show them" rather than pretending
    they do not exist.
    """
    items = find_twins(existing_items, compare_items(existing_items, plan, seen))
    materials = compare_files(existing_materials, files, folder_for, seen)
    categories = plan.get("categories") or []
    shown_items = [i for i in items if i["change"] != "dismissed"]
    shown_files = [f for f in materials if f["change"] != "dismissed"]
    return {
        "categories": categories,
        "weightsFromCanvas": bool(plan.get("weightsFromCanvas")),
        "items": shown_items,
        "files": shown_files,
        "dismissed": {
            "items": [i for i in items if i["change"] == "dismissed"],
            "files": [f for f in materials if f["change"] == "dismissed"],
        },
        "summary": {
            "newItems": sum(1 for i in shown_items if i["change"] == "new"),
            "changedItems": sum(1 for i in shown_items if i["change"] == "changed"),
            "sameItems": sum(1 for i in shown_items if i["change"] == "same"),
            "newFiles": sum(1 for f in shown_files if f["change"] == "new"),
            "conflictFiles": sum(1 for f in shown_files if f["change"] == "changed"),
            "sameFiles": sum(1 for f in shown_files if f["change"] == "same"),
            "dismissed": len(items) - len(shown_items) + len(materials) - len(shown_files),
            "categories": len(categories),
        },
    }


def nothing_to_do(draft):
    """Whether this sync would change anything at all, for the one-line answer."""
    s = draft["summary"]
    return not (s["newItems"] or s["changedItems"] or s["newFiles"] or s["conflictFiles"])


# ---------------------------------------------------------------------------
# what gets fetched now and what stays a link
# ---------------------------------------------------------------------------

# Saif's call on 2026-09-22, with his own library in front of us: 70% of his 88 Canvas
# files are under 5 MB but only 9% of the bytes, so this fetches most of his material
# for 68 MB rather than 766. Everything bigger stays a link until he opens it.
PREFETCH_MAX_BYTES = 5 * 1024 * 1024

# Video is never fetched at any size. 283 MB of his 766 is mp4, none of it yields text
# to search or to give Headstart, so a copy on the volume buys nothing at all.
VIDEO_EXTENSIONS = ("mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv", "flv")


def is_video(entry):
    mimetype = (entry.get("mimetype") or "").lower()
    if mimetype.startswith("video/"):
        return True
    name = (entry.get("filename") or entry.get("title") or "").lower()
    return name.rsplit(".", 1)[-1] in VIDEO_EXTENSIONS if "." in name else False


def should_prefetch(entry, max_bytes=PREFETCH_MAX_BYTES):
    """Whether this file's bytes are worth having before he asks for them.

    A file with no size reported is fetched: Canvas usually gives one, and the ones
    that do not are small in practice. Guessing the other way would leave a syllabus
    unsearchable.
    """
    if is_video(entry):
        return False
    size = canvas._number(entry.get("size"))
    return size is None or size <= max_bytes


# ---------------------------------------------------------------------------
# stored state: the token, the course mapping, and what has been synced before
# ---------------------------------------------------------------------------

SETTINGS_KEY = "canvas"

DEFAULT_STATE = {
    "host": "",
    "token": "",
    "autoSync": True,
    "courses": {},        # canvas course id -> {classId, lastSync, lastError, seen: []}
    "lastSync": None,
    "lastError": None,
}


def load_state(conn):
    """Everything this feature remembers, merged over the defaults.

    It lives in `app_settings` under its own key rather than in the `prefs` blob,
    because that blob is served to the browser whole and this one holds a credential.
    `app_settings` is already keyed per user and already covered by row level security,
    so each account keeps its own Canvas connection with no new table and no new
    policy, the same reasoning `prefs.py` gives for living there.
    """
    import json

    raw = db_get_setting(conn, SETTINGS_KEY)
    state = dict(DEFAULT_STATE)
    state["courses"] = {}
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = {}
        if isinstance(stored, dict):
            for key, value in stored.items():
                state[key] = value
    return state


def save_state(conn, state):
    import json

    db_set_setting(conn, SETTINGS_KEY, json.dumps(state))


def db_get_setting(conn, key):
    import db
    return db.get_setting(conn, key, "")


def db_set_setting(conn, key, value):
    import db
    db.set_setting(conn, key, value)


def public_state(state):
    """What the browser is allowed to know: everything except the token itself.

    `connected` rather than the value, because a token round-tripping through the page
    is a token in a browser history, a screenshot and a support conversation. Nothing
    in the interface needs to read it back; the only useful question is whether one is
    there.
    """
    return {
        "connected": bool((state.get("token") or "").strip()),
        "host": state.get("host") or "",
        "name": state.get("name") or "",
        "autoSync": bool(state.get("autoSync", True)),
        "lastSync": state.get("lastSync"),
        "lastError": state.get("lastError"),
        "courses": {cid: {"classId": c.get("classId"),
                          "lastSync": c.get("lastSync"),
                          "lastError": c.get("lastError")}
                    for cid, c in (state.get("courses") or {}).items()},
    }


def course_state(state, course_id):
    return (state.get("courses") or {}).get(str(course_id)) or {}


def seen_keys(state, course_id):
    """The Canvas keys previous syncs wrote for this course."""
    return set(course_state(state, course_id).get("seen") or [])


def remember(state, course_id, class_id, keys, error=None):
    """Record what a sync wrote, so the next one can tell a deletion from a novelty."""
    from datetime import datetime

    courses = state.setdefault("courses", {})
    entry = courses.setdefault(str(course_id), {})
    entry["classId"] = class_id
    entry["seen"] = sorted(set(entry.get("seen") or []) | set(keys or []))
    entry["lastSync"] = datetime.utcnow().isoformat()
    entry["lastError"] = error
    return state


def forget(state, course_id, keys):
    """Drop keys from the memory of what was synced, which un-hides them.

    This is what "show the ones I deleted" does: the rows come back as new on the next
    sync, because as far as this feature is concerned they were never written.
    """
    entry = (state.get("courses") or {}).get(str(course_id))
    if entry:
        entry["seen"] = sorted(set(entry.get("seen") or []) - set(keys or []))
    return state


# ---------------------------------------------------------------------------
# applying the ticked parts
# ---------------------------------------------------------------------------

def folder_by_name(conn, class_id, name):
    """The class's folder with this name, created if it is not there yet.

    Canvas module names make better folders than the Canvas folder tree does, because
    a course that hides its Files tab usually has no folder tree worth importing and
    its modules are the weeks: "Week 1: Introduction", "Welcome Package". Created as
    `kind='custom'`, so it behaves exactly like a folder he made himself and can be
    renamed, moved or deleted without this feature caring.
    """
    import uuid
    from datetime import datetime

    if not class_id or not (name or "").strip():
        return None
    name = name.strip()[:120]
    row = conn.execute(
        "SELECT id FROM file_folders WHERE class_id=? AND parent_id IS NULL"
        " AND lower(name)=lower(?)", (class_id, name)).fetchone()
    if row:
        return row["id"]
    fid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO file_folders (id, class_id, parent_id, name, kind, sort_order, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (fid, class_id, None, name, "custom", 50, datetime.utcnow().isoformat()))
    return fid


# Which draft field maps to which column when a single ticked change is applied.
CHANGE_COLUMNS = {"dueDate": "due_date", "dueTime": "due_time", "type": "type",
                  "weight": "weight", "score": "score", "notes": "notes"}


def _ticked(change):
    """Whether one field-level change should be applied.

    The screen sends `include` per change. Where it has not said, a fill is applied and
    a disagreement is not, which is the same default `compare_items` computed.
    """
    if "include" in change:
        return bool(change["include"])
    return bool(change.get("fill"))


def apply_draft(conn, class_id, draft, guess_category=None, journal=None):
    """Write the ticked parts of a reviewed draft into one class.

    Assumes it is already inside a transaction; the caller commits, so a half-applied
    sync cannot be left behind. Returns `(counts, to_fetch, keys)`:

    * `counts` for the message afterwards,
    * `to_fetch` the files whose bytes are worth having now, for the caller to download
      outside the transaction, because a download holds a connection open for seconds
      and SQLite answers that with "database is locked",
    * `keys` every Canvas key written, for `remember`, which is what lets the next sync
      tell an assignment he deleted from one he has never seen.

    Nothing is deleted here, and nothing not ticked is touched.

    `journal`, when given, collects one entry per write with what it replaced, which
    is everything `undo_apply` needs to reverse this exactly. See `undo_apply`.
    """
    log = journal.append if journal is not None else (lambda entry: None)
    import uuid
    from datetime import datetime

    import db

    now = datetime.utcnow().isoformat()
    counts = {"items": 0, "updated": 0, "files": 0, "categories": 0, "folders": 0}
    to_fetch, keys = [], []

    # Categories first: an item's category id is needed before the item is written.
    cat_ids = {}
    existing_cats = {(r["name"] or "").lower(): r["id"] for r in conn.execute(
        "SELECT id, name FROM grade_categories WHERE class_id=?", (class_id,))}
    for order, cat in enumerate(draft.get("categories") or []):
        if cat.get("include") is False:
            continue
        name = (cat.get("name") or "").strip()
        if not name:
            continue
        gid = existing_cats.get(name.lower())
        if gid:
            # A category he already has keeps its weight unless that exact change was
            # reviewed and accepted. Creating a missing one is structural and follows
            # the items that need it; rewriting an existing one is a decision.
            if cat.get("applyWeight"):
                old = conn.execute("SELECT weight, drop_lowest FROM grade_categories WHERE id=?",
                                   (gid,)).fetchone()
                after = {"weight": cat.get("weight"), "drop_lowest": int(cat.get("dropLowest") or 0)}
                conn.execute("UPDATE grade_categories SET weight=?, drop_lowest=? WHERE id=?",
                             (after["weight"], after["drop_lowest"], gid))
                log({"op": "cat~", "id": gid, "class": class_id,
                     "before": {"weight": _col(old, "weight"), "drop_lowest": _col(old, "drop_lowest")},
                     "after": after})
        else:
            gid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO grade_categories (id, class_id, name, weight, drop_lowest,"
                " sort_order, created_at) VALUES (?,?,?,?,?,?,?)",
                (gid, class_id, name, cat.get("weight"), int(cat.get("dropLowest") or 0),
                 order, now))
            counts["categories"] += 1
            log({"op": "cat+", "id": gid, "class": class_id, "name": name})
        cat_ids[cat.get("canvasId")] = gid

    for item in draft.get("items") or []:
        if item.get("change") == "dismissed" or not item.get("include"):
            continue
        cat = cat_ids.get(item.get("categoryCanvasId"))
        key = item.get("importKey")

        if item.get("existingId"):
            # Only the fields that were ticked. Title, status, subtasks and anything
            # else he has touched are never rewritten by a sync of an assignment that
            # already exists.
            sets, values = [], []
            current = conn.execute("SELECT * FROM items WHERE id=? AND class_id=?",
                                   (item["existingId"], class_id)).fetchone()
            fields = {}
            for change in item.get("changes") or []:
                column = CHANGE_COLUMNS.get(change.get("field"))
                if column and _ticked(change):
                    sets.append(column + "=?")
                    values.append(change.get("after"))
                    fields[column] = [_col(current, column), change.get("after")]
            # No category_id here. Accepting a moved deadline must not also move the
            # assignment into Canvas's grade category: that is a second change he was
            # never shown. Only a new assignment arrives in a category.
            # Always claim the row, even when no field changed: the key is what makes
            # the next sync exact rather than dependent on the title still matching.
            sets.append("import_key=?")
            values.append(key)
            conn.execute("UPDATE items SET " + ", ".join(sets) + " WHERE id=? AND class_id=?",
                         tuple(values) + (item["existingId"], class_id))
            counts["updated"] += 1
            fields["import_key"] = [_col(current, "import_key"), key]
            log({"op": "item~", "id": item["existingId"], "class": class_id,
                 "title": _col(current, "title"), "fields": fields})
        else:
            iid = str(uuid.uuid4())
            wrote = {"title": item.get("title") or "Untitled",
                     "type": item.get("type") or "assignment",
                     "due_date": item.get("dueDate"), "due_time": item.get("dueTime"),
                     "status": "todo", "weight": None if cat else item.get("weight"),
                     "score": item.get("score"), "notes": item.get("notes") or "",
                     "category_id": cat}
            conn.execute(
                "INSERT INTO items (id, semester_id, class_id, title, type, due_date,"
                " due_time, status, weight, score, notes, created_at, category_id,"
                " import_key, location) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, db.semester_for(conn, class_id), class_id,
                 wrote["title"], wrote["type"], wrote["due_date"], wrote["due_time"],
                 "todo", wrote["weight"], wrote["score"], wrote["notes"], now, cat, key, ""))
            counts["items"] += 1
            log({"op": "item+", "id": iid, "class": class_id, "wrote": wrote})
        if key:
            keys.append(key)

    folders = {}
    for entry in draft.get("files") or []:
        if entry.get("change") == "dismissed" or not entry.get("include"):
            continue
        name = entry.get("folderName")
        if name and name not in folders:
            had = conn.execute("SELECT id FROM file_folders WHERE class_id=? AND parent_id IS NULL"
                               " AND lower(name)=lower(?)", (class_id, name.strip()[:120])).fetchone()
            folders[name] = folder_by_name(conn, class_id, name)
            if folders[name] and not had:
                counts["folders"] += 1
                log({"op": "folder+", "id": folders[name], "class": class_id})
        mid = str(uuid.uuid4())
        filename = entry.get("filename") or entry.get("title") or "file"
        category = guess_category(filename) if guess_category else None
        # A link, deliberately: the row is real and searchable by name straight away,
        # and the bytes follow either from the prefetch below or the first time it is
        # opened. `stored_name` staying null is what marks it as not fetched yet.
        conn.execute(
            "INSERT INTO materials (id, semester_id, class_id, category, folder_id,"
            " title, kind, url, filename, stored_name, mimetype, size, extracted_text,"
            " import_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, db.semester_for(conn, class_id), class_id, category, folders.get(name),
             entry.get("title") or filename, "link", entry.get("url"), filename, None,
             entry.get("mimetype"), entry.get("size"), None, entry.get("importKey"), now))
        counts["files"] += 1
        log({"op": "mat+", "id": mid, "class": class_id,
             "wrote": {"folder_id": folders.get(name), "title": entry.get("title") or filename}})
        if entry.get("importKey"):
            keys.append(entry["importKey"])
        if should_prefetch(entry):
            to_fetch.append({"materialId": mid, "url": entry.get("url"),
                             "filename": filename, "size": entry.get("size")})

    return counts, to_fetch, keys


# ===========================================================================
# checking and reviewing
#
# Saif's design, 2026-09-22: nothing Canvas finds goes into Vesta until he has seen
# it. So the work is split in two, and the split is the whole idea:
#
# * **checking** talks to Canvas and stores what it found as a snapshot per course.
#   It is slow (seconds, dozens of requests), runs in the background when he opens
#   Vesta and once a day, and never writes a single row of his classes.
# * **reviewing** holds that snapshot up against his classes *as they are right now*.
#   It is local, pure and fast, so it runs fresh every time the banner, a class strip
#   or the review screen asks. A review can never be stale on the Vesta side, because
#   there is no stored review to go stale: an edit he made five minutes ago is already
#   reflected in it.
#
# Every change in a review has an id built from what it would do: the course, the
# assignment or file, the field, and the before and after values. An accepted id is
# re-found in a freshly computed review at the moment of applying. If anything moved
# in between, on either side, the id no longer exists and that change is skipped and
# reported rather than written over something he has not seen.
# ===========================================================================

SNAPSHOT_PREFIX = "canvas_snapshot:"

# How old the last check can be before opening Vesta starts another. "A few hours".
STALE_AFTER_HOURS = 3

# How often the daily job actually checks an account. It wakes hourly and skips any
# account checked more recently than this, so a restart does not re-check everyone.
DAILY_AFTER_HOURS = 20

# The review's groups, in the order they are shown. Moved deadlines and grades lead,
# because those are the changes with consequences; files trail, because they are the
# changes with volume.
GROUPS = (
    ("twin", "Possible duplicates"),
    ("categories", "Grade categories that overlap"),
    ("moved", "Deadlines that moved"),
    ("grade", "Grades"),
    ("newItem", "New assignments"),
    ("dateAdded", "Due dates and times Canvas can fill in"),
    ("description", "Descriptions Canvas can fill in"),
    ("layout", "Descriptions to lay out like Canvas"),
    ("newFile", "New files"),
    ("updatedFile", "Files the professor replaced"),
    ("conflict", "Files with the same name as one you have"),
    ("other", "Other changes"),
)


def _now():
    from datetime import datetime
    return datetime.utcnow().isoformat()


def _hours_since(iso):
    from datetime import datetime
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return (datetime.utcnow() - then).total_seconds() / 3600.0


def _digest(*parts):
    import hashlib
    import json
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------

def save_snapshot(conn, course_id, snapshot):
    """What the last check found for one course.

    One `app_settings` row per course rather than one blob for all of them, so a check
    of one course rewrites only that course, and a large course does not make every
    other read pay for it. Per user and under row level security like the rest.
    """
    import json
    db_set_setting(conn, SNAPSHOT_PREFIX + str(course_id), json.dumps(snapshot))


def load_snapshot(conn, course_id):
    import json
    raw = db_get_setting(conn, SNAPSHOT_PREFIX + str(course_id))
    if not raw:
        return None
    try:
        snap = json.loads(raw)
    except ValueError:
        return None
    return snap if isinstance(snap, dict) else None


# The only fields of a Canvas file worth keeping between a check and a review.
SNAPSHOT_FILE_FIELDS = ("id", "display_name", "size", "content-type", "url", "_module",
                        "modified_at", "_stub")


def _known_file_ids(conn, class_id):
    """Canvas file ids this class already holds, as the ints Canvas uses."""
    out = set()
    if not class_id:
        return out
    # The pattern is a parameter, never a literal: a bare % inside the SQL text is a
    # format character to psycopg on the way to Postgres.
    for row in conn.execute("SELECT import_key FROM materials WHERE class_id=?"
                            " AND import_key LIKE ?", (class_id, "canvas:file:%")):
        tail = (_col(row, "import_key") or "").rsplit(":", 1)[-1]
        try:
            out.add(int(tail))
        except ValueError:
            out.add(tail)
    return out


def account_zone(client):
    """The time zone his Canvas account shows deadlines in, as (tzinfo, name).

    Asked once per check. A profile Canvas will not give, or a zone name this server's
    database does not know, falls back to Vesta's own zone rather than failing the
    check: an hour's disagreement is a nuisance, and no check at all is worse.
    """
    import ics
    try:
        name = (client.profile() or {}).get("time_zone") or ""
    except Exception:
        name = ""
    tz = ics.zone(name) if name else None
    return (tz, name) if tz is not None else (None, "")


def check_course(conn, client, course_id, class_id, full=False, tz=None, tz_name=""):
    """Read one course off Canvas and store what was found. Writes no class rows."""
    course = client.course(course_id)
    groups = client.assignment_groups(course_id)
    plan = canvas.plan_course(course, groups, tz)
    known = None if full else _known_file_ids(conn, class_id)
    files = client.course_files(course_id, known=known)
    snapshot = {
        "courseId": course_id,
        "fetchedAt": _now(),
        "full": bool(full),
        "timeZone": tz_name,
        "course": {k: course.get(k) for k in
                   ("id", "name", "course_code", "apply_assignment_group_weights")},
        "plan": plan,
        "files": [{k: f.get(k) for k in SNAPSHOT_FILE_FIELDS if k in f} for f in files],
    }
    save_snapshot(conn, course_id, snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# the background check
# ---------------------------------------------------------------------------

_locks = {}
_locks_guard = None


def _lock_for(user_id):
    """One check at a time per account, in this process.

    The button and the timer can land together, and two checks would both read Canvas
    and race to save. Gunicorn runs one worker here (see the Procfile), so a process
    lock is a real lock; with more workers the worst case is one redundant check,
    because checks write only snapshots and never touch his classes.
    """
    global _locks_guard
    import threading
    if _locks_guard is None:
        _locks_guard = threading.Lock()
    with _locks_guard:
        return _locks.setdefault(user_id or "local", threading.Lock())


def is_checking(user_id):
    lock = _lock_for(user_id)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def class_is_live(conn, class_id):
    """Whether a class exists and sits in a term that is not archived.

    A background check never passes through `_guard_archived_semester`, which is a
    `before_request`, so it has to ask for itself. An archived term's classes are a
    finished record, and a sync has no business in them.
    """
    if not class_id:
        return False
    row = conn.execute(
        "SELECT s.status AS status FROM classes c LEFT JOIN semesters s ON s.id = c.semester_id"
        " WHERE c.id=?", (class_id,)).fetchone()
    return bool(row) and (_col(row, "status") or "active") != "archived"


def check_account(user_id, full=False, course_id=None, reason=""):
    """Check every mapped course for one account. Background-safe.

    Returns a small report. Never raises for a Canvas failure: a background job with
    nobody watching must record the failure where the interface will show it, which is
    `lastError` on the account and on the course.
    """
    import db

    lock = _lock_for(user_id)
    if not lock.acquire(blocking=False):
        return {"busy": True}
    report = {"checked": 0, "errors": {}, "reason": reason}
    try:
        conn = db.get_db(user_id=user_id)
        try:
            state = load_state(conn)
            if not (state.get("token") or "").strip():
                return {"connected": False}
            results, account_error = {}, None
            try:
                client = canvas.Client(state.get("host"), state.get("token"))
            except canvas.CanvasError as e:
                client, account_error = None, e.message
            tz, tz_name = account_zone(client) if client is not None else (None, "")
            for cid, entry in list((state.get("courses") or {}).items()):
                if client is None:
                    break
                if course_id is not None and str(course_id) != str(cid):
                    continue
                class_id = entry.get("classId")
                if not class_is_live(conn, class_id):
                    continue
                try:
                    check_course(conn, client, cid, class_id, full=full, tz=tz,
                                 tz_name=tz_name)
                    results[cid] = None
                    report["checked"] += 1
                except canvas.CanvasError as e:
                    results[cid] = e.message
                    report["errors"][cid] = e.message
                    if e.needs_token or e.rate_limited:
                        # Every course after this one fails the same way.
                        account_error = e.message
                        break
            # Re-read before saving. He may have changed the mapping or snoozed the
            # banner while Canvas was answering, and the state read at the start of a
            # twenty-second check must not overwrite that.
            fresh = load_state(conn)
            stamp = _now()
            fresh["lastCheck"] = stamp
            fresh["lastError"] = account_error
            for cid, err in results.items():
                entry = (fresh.get("courses") or {}).get(cid)
                if entry is not None:
                    entry["lastCheck"] = stamp
                    entry["lastError"] = err
            save_state(conn, fresh)
        finally:
            conn.close()
    finally:
        lock.release()
    return report


def is_stale(state, hours=STALE_AFTER_HOURS):
    age = _hours_since(state.get("lastCheck"))
    return age is None or age >= hours


def start_daily(interval_seconds=3600, first_delay_seconds=600):
    """Wake hourly and check any account not checked in the last day.

    There is no scheduler elsewhere in this app to hang this on: Google Calendar sync
    is driven by Google calling us. The first wake is ten minutes after boot, so a
    deploy does not open with a burst of Canvas traffic, and so a test process that
    imports the app never reaches a real check. `VESTA_NO_BACKGROUND=1` turns it off.

    This is the backup, not the main path. The main path is the check that starts when
    he opens Vesta and the last one is more than a few hours old, which is fresher
    exactly when it matters. This one exists so a review is already waiting when he
    opens Vesta on his phone after a day away.
    """
    import os
    import threading
    import time

    import db

    if os.environ.get("VESTA_NO_BACKGROUND") == "1":
        return None

    def one(user_id):
        try:
            conn = db.get_db(user_id=user_id)
            try:
                state = load_state(conn)
            finally:
                conn.close()
            if not state.get("token") or not state.get("autoSync", True):
                return
            if not is_stale(state, DAILY_AFTER_HOURS):
                return
            check_account(user_id, reason="daily")
        except Exception:
            # One account's failure must not end the loop for everyone else. The
            # failures that matter are already recorded in that account's state.
            pass

    def loop():
        time.sleep(first_delay_seconds)
        while True:
            try:
                db.for_each_account(one)
            except Exception:
                pass
            time.sleep(interval_seconds)

    t = threading.Thread(target=loop, name="canvas-daily", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# the review
# ---------------------------------------------------------------------------

def _fmt_date(iso):
    from datetime import datetime
    if not iso:
        return "no date"
    try:
        d = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
    except ValueError:
        return str(iso)
    return "%s %d" % (d.strftime("%b"), d.day)


def _fmt_time(hhmm):
    if not hhmm:
        return ""
    try:
        h, m = [int(x) for x in str(hhmm).split(":")[:2]]
    except ValueError:
        return str(hhmm)
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return ("%d%s" % (h12, suffix)) if m == 0 else ("%d:%02d%s" % (h12, m, suffix))


def _fmt_due(date, time):
    return (_fmt_date(date) + (" " + _fmt_time(time) if time else "")).strip()


def _reject_key(unit):
    return unit["_rejectKey"]


def _is_rejected(entry, reject_key, after):
    return (entry.get("rejected") or {}).get(reject_key) == _digest(after)


def _units_for_class(draft, existing_cats, course_entry, course_id, class_id, class_label):
    """Turn one class's draft into the review's units: one accept-or-keep decision each.

    A unit is the smallest thing that makes sense to decide on its own. A moved
    deadline is one unit even when both the date and the time moved, because nobody
    wants to accept the new date and keep the old time. A file conflict is one unit
    whose accept means "add Canvas's copy too" and whose refusal means "keep mine".
    """
    units = []
    rejected_entry = course_entry or {}

    def add(kind, key, title, reject_key, before, after, **extra):
        if _is_rejected(rejected_entry, reject_key, after):
            return
        unit = {
            "id": kind + ":" + _digest(course_id, key, reject_key, before, after),
            "kind": kind, "classId": class_id, "courseId": str(course_id),
            "classLabel": class_label, "key": key, "title": title,
            "before": before, "after": after, "_rejectKey": reject_key,
        }
        unit.update(extra)
        units.append(unit)

    for item in draft["items"]:
        key, title = item["importKey"], item["title"]
        twin = item.get("twin")
        if twin and not _is_rejected(rejected_entry, key + "|twin", twin["mineId"]):
            # Until he says whether they are one assignment, nothing else about it is
            # worth asking: its date or grade may be about to belong to his row.
            add("twin", key, title, key + "|twin", None, twin["mineId"],
                mine={"title": twin["mineTitle"],
                      "due": _fmt_due(twin["mineDue"], twin["mineTime"]) if twin["mineDue"] else None,
                      "done": twin["mineDone"]},
                theirs={"due": _fmt_due(item.get("dueDate"), item.get("dueTime"))
                        if item.get("dueDate") else None},
                alreadyHere=bool(twin.get("copyId")), _item=item)
            continue
        if item["change"] == "new":
            add("newItem", key, title, key + "|new", None,
                {"type": item.get("type"), "due": _fmt_due(item.get("dueDate"), item.get("dueTime")),
                 "dueDate": item.get("dueDate"), "dueTime": item.get("dueTime"),
                 "score": item.get("score"),
                 "weight": item.get("weight") if draft.get("weightsFromCanvas") else None},
                _item=item)
            continue
        if item["change"] != "changed":
            continue
        title = item.get("existingTitle") or title
        by_field = {c["field"]: c for c in item["changes"]}
        due_changes = [by_field[f] for f in ("dueDate", "dueTime") if f in by_field]
        if due_changes:
            before = (_col_before(by_field, "dueDate", item, "before"),
                      _col_before(by_field, "dueTime", item, "before"))
            after = (by_field["dueDate"]["after"] if "dueDate" in by_field else before[0],
                     by_field["dueTime"]["after"] if "dueTime" in by_field else before[1])
            had_date = bool(before[0])
            # Only a real disagreement is a move. Canvas supplying a time Vesta never had
            # (Sep 13 becoming Sep 13 11:59pm) is filling a gap, not moving a deadline.
            filling = all(c.get("fill") for c in due_changes)
            add("dateAdded" if filling else "moved", key, title, key + "|due",
                {"date": before[0], "time": before[1], "text": _fmt_due(*before) if had_date else None},
                {"date": after[0], "time": after[1], "text": _fmt_due(*after)},
                _item=item, _changes=due_changes)
        if "notes" in by_field:
            c = by_field["notes"]
            add("layout" if c.get("relayout") else "description", key, title, key + "|notes",
                c["before"], c["after"], _item=item, _changes=[c])
        if "score" in by_field:
            c = by_field["score"]
            add("grade", key, title, key + "|score", c["before"], c["after"],
                _item=item, _changes=[c])
        for field in ("type", "weight"):
            if field in by_field:
                c = by_field[field]
                add("other", key, title, key + "|" + field, c["before"], c["after"],
                    field=field, _item=item, _changes=[c])

    for f in draft["files"]:
        key, title = f["importKey"], f["title"]
        if f["change"] == "new":
            add("newFile", key, title, key + "|new", None,
                {"size": f.get("size"), "folder": f.get("folderName"),
                 "fetchNow": should_prefetch(f)}, _file=f)
        elif f["change"] == "changed":
            add("conflict", key, title, key + "|conflict",
                {"size": f["conflict"]["existingSize"], "title": f["conflict"]["existingTitle"]},
                {"size": f["conflict"]["canvasSize"]},
                _file=f, acceptLabel="Add Canvas's copy too", keepLabel="Keep mine")
        elif f["change"] == "updated":
            add("updatedFile", key, title, key + "|update",
                {"size": f["conflict"]["existingSize"]}, {"size": f["conflict"]["canvasSize"]},
                _file=f, acceptLabel="Update", keepLabel="Keep this version")

    # A category he already has whose weight Canvas disagrees with. Creating a
    # category he does not have is not a unit: it follows the new assignments that
    # need it, and makes no sense on its own.
    have = {(_col(r, "name") or "").lower(): r for r in existing_cats or []}
    for cat in draft.get("categories") or []:
        row = have.get((cat.get("name") or "").lower())
        if not row:
            continue
        before = (canvas._number(_col(row, "weight")), int(_col(row, "drop_lowest") or 0))
        after = (cat.get("weight"), int(cat.get("dropLowest") or 0))
        if before[0] is not None and after[0] is not None and round(before[0], 4) == round(after[0], 4) \
                and before[1] == after[1]:
            continue
        add("other", "category:" + cat["name"].lower(), cat["name"] + " (grade category)",
            "category:" + cat["name"].lower() + "|weight",
            {"weight": before[0], "dropLowest": before[1]},
            {"weight": after[0], "dropLowest": after[1]},
            field="categoryWeight", _category=cat)
    return units


def _col_before(by_field, field, item, which):
    c = by_field.get(field)
    if c is not None:
        return c.get(which)
    # the field did not change, so its current value is Canvas's value too
    return item.get(field)


def _group(units):
    out = []
    for kind, label in GROUPS:
        members = [u for u in units if u["kind"] == kind]
        if members:
            out.append({"kind": kind, "label": label, "count": len(members),
                        "changes": members})
    return out


def _headlines(units, limit=3):
    """The banner's words: what happened, not how many things happened.

    A moved deadline is named individually, because "PHIL110 midterm moved Oct 19 to
    Oct 21" is the sentence worth reading on arrival. Everything else is counted by
    kind. Three at most, in the groups' order of importance.
    """
    lines = []
    twins = sum(1 for u in units if u["kind"] == "twin")
    if twins:
        lines.append("%d possible duplicate%s" % (twins, "" if twins == 1 else "s"))
    for u in units:
        if u["kind"] == "categories":
            lines.append("%s's grade weights add up to %s%%" % (u["classLabel"], _pct(u["after"]["total"])))
    moved = [u for u in units if u["kind"] == "moved"]
    for u in moved[:2]:
        lines.append("%s %s moved %s to %s" % (u["classLabel"], u["title"],
                                               u["before"]["text"], u["after"]["text"]))
    if len(moved) > 2:
        lines.append("%d more deadlines moved" % (len(moved) - 2))
    # A grade Vesta did not have is news; one that disagrees with his is a question.
    # "1 new grade" for a score he already typed would undersell it.
    fresh = sum(1 for u in units if u["kind"] == "grade" and u["before"] is None)
    differs = sum(1 for u in units if u["kind"] == "grade" and u["before"] is not None)
    if fresh:
        lines.append("%d new grade%s" % (fresh, "" if fresh == 1 else "s"))
    if differs:
        lines.append("%d grade%s that differ%s from yours" % (
            differs, "" if differs == 1 else "s", "s" if differs == 1 else ""))
    counted = (("newItem", "new assignment", "new assignments"),
               ("dateAdded", "due date to fill in", "due dates to fill in"),
               ("description", "description to fill in", "descriptions to fill in"),
               ("layout", "description to lay out", "descriptions to lay out"),
               ("newFile", "new file", "new files"),
               ("updatedFile", "replaced file", "replaced files"),
               ("conflict", "file to compare", "files to compare"),
               ("other", "other change", "other changes"))
    for kind, one, many in counted:
        n = sum(1 for u in units if u["kind"] == kind)
        if n:
            lines.append("%d %s" % (n, one if n == 1 else many))
    return lines[:limit]


def _pct(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")


def _fingerprint(units):
    return _digest(sorted(u["id"] for u in units)) if units else None


def build_review(conn, state, class_id=None):
    """The review, computed now, against his classes as they are now.

    Includes private `_` fields the apply path needs; `public_review` strips them.
    """
    classes, all_units = [], []
    snoozed = state.get("snoozed") or {}
    for cid, entry in (state.get("courses") or {}).items():
        cls_id = entry.get("classId")
        if not cls_id or (class_id and cls_id != class_id):
            continue
        if not class_is_live(conn, cls_id):
            continue
        snap = load_snapshot(conn, cid)
        if not snap:
            continue
        cls = conn.execute("SELECT code, name FROM classes WHERE id=?", (cls_id,)).fetchone()
        label = (_col(cls, "code") or _col(cls, "name") or "This class") if cls else "This class"
        items = conn.execute("SELECT * FROM items WHERE class_id=?", (cls_id,)).fetchall()
        materials = conn.execute("SELECT * FROM materials WHERE class_id=?", (cls_id,)).fetchall()
        cats = conn.execute("SELECT * FROM grade_categories WHERE class_id=?", (cls_id,)).fetchall()
        draft = compare(items, materials, snap.get("plan") or {}, snap.get("files") or [],
                        seen=seen_keys(state, cid))
        units = _units_for_class(draft, cats, entry, cid, cls_id, label)
        # Overlapping categories are only worth deciding once duplicates are settled:
        # merging one can empty one of Canvas's categories, which then goes on its own.
        if not any(u["kind"] == "twin" for u in units):
            ov = category_overlap(items, cats, snap.get("plan") or {})
            if ov:
                units.append({
                    "id": "categories:" + _digest(cid, cls_id, ov),
                    "kind": "categories", "classId": cls_id, "courseId": str(cid),
                    "classLabel": label, "key": "categories:" + cls_id,
                    "title": "Grade categories in " + label, "before": None, "after": ov,
                    "_rejectKey": cls_id + "|categories"})
        fp = _fingerprint(units)
        dismissed = draft["dismissed"]["items"] + draft["dismissed"]["files"]
        classes.append({
            "classId": cls_id, "courseId": str(cid), "label": label,
            "fetchedAt": snap.get("fetchedAt"), "lastError": entry.get("lastError"),
            "weightsFromCanvas": draft["weightsFromCanvas"],
            "count": len(units), "fingerprint": fp,
            "headlines": _headlines(units),
            "snoozed": bool(fp) and snoozed.get(cls_id) == fp,
            "hidden": len(dismissed),
            "groups": _group(units),
            "_dismissedKeys": [d["importKey"] for d in dismissed],
        })
        all_units.extend(units)
    fp = _fingerprint(all_units)
    return {
        "count": len(all_units),
        "fingerprint": fp,
        "snoozed": bool(fp) and snoozed.get("all") == fp,
        "headlines": _headlines(all_units),
        "groups": _group(all_units),
        "classes": classes,
    }


def public_review(review):
    """The review with every private field removed, for the browser."""
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if not str(k).startswith("_")}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return clean(review)


def review_summary(review):
    """Enough for the banner and the class strips, without every row."""
    return {
        "count": review["count"],
        "fingerprint": review["fingerprint"],
        "snoozed": review["snoozed"],
        "headlines": review["headlines"],
        "classes": [{"classId": c["classId"], "label": c["label"], "count": c["count"],
                     "headlines": c["headlines"],
                     "fingerprint": c["fingerprint"], "snoozed": c["snoozed"],
                     "hidden": c["hidden"], "lastError": c["lastError"],
                     "fetchedAt": c["fetchedAt"]} for c in review["classes"]],
    }


# ---------------------------------------------------------------------------
# acting on a review
# ---------------------------------------------------------------------------

def _reject(state, unit):
    """Remember "keep mine", so the same Canvas value is never offered again.

    Remembered against the value Canvas offered, not the assignment: if Canvas later
    changes that field to something else, that is news, and it is offered again.
    Refusing a new assignment or a new file is the same as deleting one after it
    arrived, so it goes where deletions go, and "show hidden" brings it back.
    """
    entry = state.setdefault("courses", {}).setdefault(unit["courseId"], {})
    if unit["kind"] in ("newItem", "newFile"):
        entry["seen"] = sorted(set(entry.get("seen") or []) | {unit["key"]})
        return
    rejected = entry.setdefault("rejected", {})
    rejected[unit["_rejectKey"]] = _digest(unit["after"])


def apply_selection(conn, state, accept, reject, guess_category=None):
    """Apply the accepted units and remember the refused ones.

    The review is recomputed here rather than trusted from the browser. An id that is
    no longer in it means something changed since he looked, on either side, and that
    change is skipped and reported, never applied against a value he has not seen.

    Returns a report plus the files to fetch, which the caller downloads after the
    transaction has committed.
    """
    import copy

    review = build_review(conn, state)
    units = {}
    for c in review["classes"]:
        for g in c["groups"]:
            for u in g["changes"]:
                units[u["id"]] = u
    accept, reject = list(accept or []), list(reject or [])
    stale = [i for i in accept + reject if i not in units]
    # What the stored state looked like before, so an undo can put it back.
    before = copy.deepcopy(state.get("courses") or {})
    journal = []

    decided_categories = []
    for uid in reject:
        if uid in units:
            if units[uid]["kind"] == "categories":
                decided_categories.append((units[uid], False))    # keep mine: an action
            else:
                _reject(state, units[uid])

    by_class = {}
    kept, merged = [], 0
    for uid in accept:
        u = units.get(uid)
        if not u:
            continue
        if u["kind"] == "twin":
            snap = load_snapshot(conn, u["courseId"]) or {}
            names = {(c.get("name") or "").lower()
                     for c in ((snap.get("plan") or {}).get("categories") or [])}
            why = _merge_twin(conn, u, journal, names)
            if why:
                kept.append({"title": u["title"], "why": why})
            else:
                merged += 1
                remember(state, u["courseId"], u["classId"], [u["key"]])
        elif u["kind"] == "categories":
            decided_categories.append((u, True))
        else:
            by_class.setdefault(u["classId"], []).append(u)
    loose = []
    for u, use_canvas in decided_categories:
        loose += _resolve_categories(conn, u, use_canvas,
                                     load_snapshot(conn, u["courseId"]) or {}, journal)

    totals = {"items": 0, "updated": 0, "files": 0, "categories": 0, "folders": 0,
              "replaced": 0}
    to_fetch = []
    for class_id, class_units in by_class.items():
        course_id = class_units[0]["courseId"]
        snap = load_snapshot(conn, course_id) or {}
        draft = _draft_from_units(class_units, snap)
        counts, fetch, keys = apply_draft(conn, class_id, draft, guess_category, journal)
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v
        to_fetch.extend(fetch)
        remember(state, course_id, class_id, keys)
        for u in class_units:
            if u["kind"] != "updatedFile":
                continue
            f = u["_file"]
            row = conn.execute("SELECT * FROM materials WHERE id=? AND class_id=?",
                               (f["existingId"], class_id)).fetchone()
            if not row:
                continue
            journal.append({"op": "mat~", "id": f["existingId"], "class": class_id,
                            "title": _col(row, "title"),
                            "before": {k: _col(row, k) for k in
                                       ("kind", "stored_name", "preview_name", "size", "url")}})
            conn.execute("UPDATE materials SET size=?, url=COALESCE(?, url) WHERE id=?",
                         (f.get("size"), f.get("url"), f["existingId"]))
            totals["replaced"] += 1
            if _col(row, "stored_name"):
                # He already had the bytes, so he should have the new ones, whatever
                # their size. The old copy stays until the new one is on disk.
                to_fetch.append({"materialId": f["existingId"], "url": f.get("url"),
                                 "filename": f.get("filename"), "size": f.get("size"),
                                 "replace": True})
    totals["merged"] = merged
    totals["categoriesResolved"] = len(decided_categories)
    rejected_count = len([i for i in reject if i in units and units[i]["kind"] != "categories"])
    report = {"applied": totals, "stale": stale, "rejected": rejected_count,
              "notMerged": kept, "leftOutOfCategories": loose}
    undo = _undo_entry(before, state.get("courses") or {}, journal, report,
                       sorted({u["classLabel"] for u in units.values()
                               if u["id"] in set(accept) | set(reject)}))
    if undo:
        report["_undo"] = undo
    return report, to_fetch


def _draft_from_units(units, snapshot):
    """Rebuild the part of a draft `apply_draft` needs from the units he accepted."""
    items, files, cats = {}, [], {}
    trust = bool((snapshot.get("plan") or {}).get("weightsFromCanvas"))
    plan_cats = {c.get("canvasId"): c for c in ((snapshot.get("plan") or {}).get("categories") or [])}

    for u in units:
        kind = u["kind"]
        if kind == "newItem":
            item = dict(u["_item"], include=True, change="new", existingId=None, changes=[])
            if not trust:
                # A points-based course's weights are not to be trusted (CANVAS.md), so a
                # new assignment from one arrives without a weight rather than with 0%.
                item["weight"] = None
            if item.get("categoryCanvasId") in plan_cats:
                cats[item["categoryCanvasId"]] = dict(plan_cats[item["categoryCanvasId"]])
            items[u["key"]] = item
        elif kind in ("moved", "dateAdded", "description", "layout", "grade", "other") and u.get("_item"):
            base = items.setdefault(u["key"], dict(u["_item"], include=True, changes=[]))
            for c in u.get("_changes") or []:
                base["changes"].append(dict(c, include=True))
        elif kind == "other" and u.get("_category"):
            cats[u["_category"].get("canvasId")] = dict(u["_category"], applyWeight=True)
        elif kind == "newFile":
            files.append(dict(u["_file"], include=True, change="new", existingId=None))
        elif kind == "conflict":
            # "Add Canvas's copy too": a new row beside his, never over it.
            files.append(dict(u["_file"], include=True, change="new", existingId=None))
    return {"items": list(items.values()), "files": files, "categories": list(cats.values())}


def snooze(state, fingerprint, class_id=None):
    """"Later": hide the banner, or one class's strip, until something new is found.

    Stored against the fingerprint of what was showing, not a time. The banner comes
    back the moment a check finds anything that was not there when he said later,
    and not before. Stored server-side so "later" on the laptop is also later on the
    phone.
    """
    state.setdefault("snoozed", {})[class_id or "all"] = fingerprint
    return state


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------

from flask import Blueprint, current_app, jsonify, request  # noqa: E402

bp = Blueprint("canvas_sync", __name__)


def _run_in_background(fn, *args):
    import threading
    threading.Thread(target=fn, args=args, daemon=True).start()


def _status(conn, state):
    import db
    review = build_review(conn, state)
    out = public_state(state)
    out["stale"] = bool(out["connected"]) and is_stale(state)
    out["checking"] = is_checking(db.current_user_id())
    out["lastCheck"] = state.get("lastCheck")
    out["review"] = review_summary(review)
    return out


@bp.route("/api/canvas", methods=["GET"])
def status():
    import db
    conn = db.get_db()
    try:
        return jsonify(_status(conn, load_state(conn)))
    finally:
        conn.close()


@bp.route("/api/canvas/connect", methods=["POST"])
def connect():
    """Verify a token against Canvas, and only then store it."""
    import db
    body = request.get_json(force=True) or {}
    host = canvas.normalise_host(body.get("host") or "canvas.sfu.ca")
    token = (body.get("token") or "").strip()
    try:
        client = canvas.Client(host, token)
        me = client.whoami()
        courses = client.courses()
    except canvas.CanvasError as e:
        return jsonify({"error": e.message, "needsToken": e.needs_token}), 400
    conn = db.get_db()
    try:
        state = load_state(conn)
        state["host"], state["token"], state["lastError"] = host, token, None
        # Not secret, and worth showing: "Connected as Saif Abuhaltam" is how he knows
        # the token he pasted was his own and not a classmate's.
        state["name"] = me.get("name") or ""
        save_state(conn, state)
        classes = conn.execute("SELECT id, code, name FROM classes WHERE semester_id=?",
                               (db.active_semester_id(conn),)).fetchall()
        suggestions = canvas.suggest_mapping(courses, [dict(r) for r in classes])
        current = {cid: e.get("classId") for cid, e in (state.get("courses") or {}).items()}
        for s in suggestions:
            s["mapped"] = current.get(str(s["courseId"]))
        return jsonify({"name": me.get("name"), "courses": suggestions})
    finally:
        conn.close()


@bp.route("/api/canvas", methods=["DELETE"])
def disconnect():
    """Drop the token. The mapping, the hidden list and the refusals are kept, so
    reconnecting later picks up exactly where he left off."""
    import db
    conn = db.get_db()
    try:
        state = load_state(conn)
        state["token"] = ""
        state["lastError"] = None
        save_state(conn, state)
        return jsonify({"ok": True})
    finally:
        conn.close()


@bp.route("/api/canvas/courses", methods=["GET"])
def courses():
    import db
    conn = db.get_db()
    try:
        state = load_state(conn)
        try:
            client = canvas.Client(state.get("host"), state.get("token"))
            found = client.courses()
        except canvas.CanvasError as e:
            return jsonify({"error": e.message, "needsToken": e.needs_token}), 400
        classes = conn.execute("SELECT id, code, name FROM classes WHERE semester_id=?",
                               (db.active_semester_id(conn),)).fetchall()
        suggestions = canvas.suggest_mapping(found, [dict(r) for r in classes])
        current = {cid: e.get("classId") for cid, e in (state.get("courses") or {}).items()}
        for s in suggestions:
            s["mapped"] = current.get(str(s["courseId"]))
        return jsonify({"courses": suggestions})
    finally:
        conn.close()


# The class colours the page offers, in its order (`PALETTE` in static/index.html).
# A class made from Canvas takes the first one no class in the term has yet, so it
# does not arrive as the only colourless card on the dashboard.
CLASS_PALETTE = ("#E0693C", "#1DA6A0", "#9B5DE0", "#8AA62E", "#E0568F", "#D9A017",
                 "#3576D9", "#34A868")


def _class_from_course(conn, course):
    """A new Vesta class for a Canvas course he has no class for yet."""
    import uuid
    import db

    semester = db.active_semester_id(conn)
    used = {(_col(r, "color") or "").upper() for r in conn.execute(
        "SELECT color FROM classes WHERE semester_id=?", (semester,))}
    free = [c for c in CLASS_PALETTE if c.upper() not in used]
    colour = free[0] if free else CLASS_PALETTE[len(used) % len(CLASS_PALETTE)]
    cid = str(uuid.uuid4())
    conn.execute("INSERT INTO classes (id, semester_id, code, name, professor, color, notes,"
                 " grade_scale, website, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (cid, semester, canvas.class_code(course), canvas.course_title(course),
                  "", colour, "", None, "", _now()))
    return cid


@bp.route("/api/canvas/mapping", methods=["PUT"])
def set_mapping():
    """Pair Canvas courses with classes. A value of "new" makes the class; null unpairs.

    Two courses into one class is refused: their assignments would interleave in one
    list and every later sync would fight over which course a row belongs to.
    """
    import db
    body = request.get_json(force=True) or {}
    mapping = body.get("mapping") or {}
    targets = [v for v in mapping.values() if v and v != "new"]
    if len(targets) != len(set(targets)):
        return jsonify({"error": "Two Canvas courses cannot share one class."}), 400
    conn = db.get_db()
    try:
        state = load_state(conn)
        known = {}
        if any(v == "new" for v in mapping.values()):
            try:
                client = canvas.Client(state.get("host"), state.get("token"))
                known = {str(c.get("id")): c for c in client.courses()}
            except canvas.CanvasError as e:
                return jsonify({"error": e.message, "needsToken": e.needs_token}), 400
        courses_state = state.setdefault("courses", {})
        for course_id, target in mapping.items():
            course_id = str(course_id)
            if target == "new":
                course = known.get(course_id)
                if not course:
                    continue
                target = _class_from_course(conn, course)
            elif target and not conn.execute("SELECT 1 FROM classes WHERE id=?",
                                             (target,)).fetchone():
                continue
            entry = courses_state.setdefault(course_id, {})
            entry["classId"] = target or None
        conn.commit()
        save_state(conn, state)
        out = public_state(state)
    finally:
        conn.close()
    # A new pairing has nothing to review until it has been checked once.
    _run_in_background(check_account, db.current_user_id(), False, None, "mapping")
    return jsonify(out)


@bp.route("/api/canvas/check", methods=["POST"])
def check():
    """Start a check in the background and return at once.

    `ifStale` is what opening Vesta sends: check only if the last one is more than a
    few hours old. `full` is "Check for updates": re-read every file, which is how a
    file the professor replaced in place gets noticed. `courseId` narrows it to one.
    """
    import db
    body = request.get_json(silent=True) or {}
    uid = db.current_user_id()
    conn = db.get_db()
    try:
        state = load_state(conn)
    finally:
        conn.close()
    if not (state.get("token") or "").strip():
        return jsonify({"started": False, "connected": False})
    if body.get("ifStale") and not is_stale(state):
        return jsonify({"started": False, "fresh": True})
    if is_checking(uid):
        return jsonify({"started": False, "checking": True})
    _run_in_background(check_account, uid, bool(body.get("full")), body.get("courseId"),
                       "full" if body.get("full") else ("open" if body.get("ifStale") else "button"))
    return jsonify({"started": True}), 202


@bp.route("/api/canvas/review", methods=["GET"])
def review():
    import db
    conn = db.get_db()
    try:
        state = load_state(conn)
        out = public_review(build_review(conn, state, request.args.get("classId")))
        out["history"] = history(conn)
        return jsonify(out)
    finally:
        conn.close()


@bp.route("/api/canvas/apply", methods=["POST"])
def apply():
    """Accept some changes and keep his own values for others, in one go."""
    import db
    body = request.get_json(force=True) or {}
    guess = current_app.config.get("GUESS_FILE_CATEGORY")
    fetch = current_app.config.get("CANVAS_FETCH")
    uid = db.current_user_id()
    conn = db.get_db()
    try:
        state = load_state(conn)
        try:
            report, to_fetch = apply_selection(conn, state, body.get("accept"),
                                               body.get("reject"), guess)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        save_state(conn, state)
        undo = report.pop("_undo", None)
        if undo:
            record_undo(conn, undo)
            report["undoId"] = undo["id"]
        report["review"] = review_summary(build_review(conn, state))
    finally:
        conn.close()
    if to_fetch and fetch:
        _run_in_background(_prefetch, fetch, uid, to_fetch)
    report["fetching"] = len(to_fetch)
    return jsonify(report)


def _prefetch(fetch, user_id, entries):
    """Fetch each file in turn. One failure leaves that file a link, and the rest go on."""
    for entry in entries:
        try:
            fetch(entry["materialId"], user_id=user_id, replace=bool(entry.get("replace")))
        except Exception:
            continue


@bp.route("/api/canvas/snooze", methods=["POST"])
def snooze_route():
    import db
    body = request.get_json(force=True) or {}
    conn = db.get_db()
    try:
        state = load_state(conn)
        snooze(state, body.get("fingerprint"), body.get("classId"))
        save_state(conn, state)
        return jsonify({"ok": True})
    finally:
        conn.close()


@bp.route("/api/canvas/unhide", methods=["POST"])
def unhide():
    """Offer again everything he deleted or refused in one class."""
    import db
    body = request.get_json(force=True) or {}
    class_id = body.get("classId")
    conn = db.get_db()
    try:
        state = load_state(conn)
        review = build_review(conn, state, class_id)
        for c in review["classes"]:
            forget(state, c["courseId"], c["_dismissedKeys"])
            entry = (state.get("courses") or {}).get(c["courseId"])
            if entry:
                entry["rejected"] = {}
        save_state(conn, state)
        return jsonify(review_summary(build_review(conn, state)))
    finally:
        conn.close()


@bp.route("/api/canvas/auto", methods=["PUT"])
def set_auto():
    """The one switch: whether Vesta checks Canvas on its own."""
    import db
    body = request.get_json(force=True) or {}
    conn = db.get_db()
    try:
        state = load_state(conn)
        state["autoSync"] = bool(body.get("autoSync"))
        save_state(conn, state)
        return jsonify(public_state(state))
    finally:
        conn.close()


# ===========================================================================
# undo
#
# Saif asked for this on 2026-09-22: a way back after Accept all, in case he changes
# his mind. Every apply records exactly what it wrote and what each write replaced;
# undoing one reverses those writes and puts the review's memory (what was synced,
# skipped or kept) back the way it was, so what was undone is offered again.
#
# The rule that makes it safe to press: **undo never destroys his work.** Anything he
# has touched since the apply is left exactly as it is and named in the report: an
# assignment he edited, finished, or hung anything off (subtasks, notes, a Headstart
# draft, a chat, a quiz, flashcards, a rubric, an attached file); a file he moved,
# renamed, linked, or used as a source; a field he changed again afterwards. Only what
# is still precisely as the apply left it is taken back.
# ===========================================================================

UNDO_KEY = "canvas_undo"

# How many applies can be undone. Ten covers "I changed my mind" by a wide margin, and
# bounds the old file copies kept for a replaced file (see `_forget_undo`).
UNDO_KEEP = 10

# Everything that can hang off an assignment or a file, as (table, column). Deleting a
# row with any of these attached would take his work with it, through a cascade or by
# quietly unlinking it, so undo leaves such a row alone. `tests/test_canvas_undo.py`
# compares these lists with the live schema, so a new table cannot be missed here.
ITEM_DEPENDENTS = (
    ("subtasks", "item_id", "it has subtasks"),
    ("notes", "linked_item_id", "a note is linked to it"),
    ("note_links", "item_id", "a note is linked to it"),
    ("headstarts", "item_id", "it has Headstart work"),
    ("rubrics", "item_id", "a rubric is linked to it"),
    ("flashcard_decks", "item_id", "a flashcard set is linked to it"),
    ("quizzes", "item_id", "a quiz is linked to it"),
    ("threads", "item_id", "a chat is linked to it"),
    ("item_files", "item_id", "files are attached to it"),
)
MATERIAL_DEPENDENTS = (
    ("note_links", "file_id", "a note links to it"),
    ("rubrics", "material_id", "it was parsed as a rubric"),
    ("headstart_sources", "material_id", "Headstart used it"),
    ("flashcard_decks", "source_material_id", "flashcards were made from it"),
    ("thread_sources", "material_id", "a chat uses it"),
    ("item_files", "material_id", "it is attached to an assignment"),
)

# What an apply wrote into a new assignment, and so what must still be there for the
# assignment to count as untouched.
ITEM_WROTE = ("title", "type", "due_date", "due_time", "status", "weight", "score",
              "notes", "category_id")


def _undo_entry(before, after, journal, report, class_labels):
    """The record an undo needs: the writes, and how the stored state changed."""
    import uuid

    seen_added, rejected_prev = {}, {}
    for cid, entry in (after or {}).items():
        old = before.get(cid) or {}
        added = sorted(set(entry.get("seen") or []) - set(old.get("seen") or []))
        if added:
            seen_added[cid] = added
        was, now = old.get("rejected") or {}, entry.get("rejected") or {}
        changed = {rk: was.get(rk) for rk in now if was.get(rk) != now[rk]}
        if changed:
            rejected_prev[cid] = changed
    if not journal and not seen_added and not rejected_prev:
        return None
    return {"id": str(uuid.uuid4()), "at": _now(), "classes": class_labels,
            "summary": _undo_summary(report), "journal": journal,
            "seenAdded": seen_added, "rejectedPrev": rejected_prev}


def _undo_summary(report):
    a = report.get("applied") or {}

    def n(count, one, many=None):
        return "%d %s" % (count, one if count == 1 else (many or one + "s"))
    parts = []
    if a.get("items"):
        parts.append("added " + n(a["items"], "assignment"))
    if a.get("updated"):
        parts.append("updated " + n(a["updated"], "assignment"))
    if a.get("files"):
        parts.append("added " + n(a["files"], "file"))
    if a.get("replaced"):
        parts.append("updated " + n(a["replaced"], "file"))
    if a.get("merged"):
        parts.append("merged " + n(a["merged"], "duplicate"))
    if a.get("categoriesResolved"):
        parts.append("settled " + n(a["categoriesResolved"], "category overlap"))
    if report.get("rejected"):
        parts.append("kept yours for " + n(report["rejected"], "change"))
    text = ", ".join(parts) or "saved your choices"
    return text[0].upper() + text[1:]


def load_undo(conn):
    import json
    raw = db_get_setting(conn, UNDO_KEY)
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        entries = []
    return entries if isinstance(entries, list) else []


def save_undo(conn, entries):
    import json
    db_set_setting(conn, UNDO_KEY, json.dumps(entries, default=str))


def record_undo(conn, entry):
    """Keep this apply undoable, newest first, and let the oldest go past UNDO_KEEP."""
    entries = [entry] + load_undo(conn)
    kept, dropped = entries[:UNDO_KEEP], entries[UNDO_KEEP:]
    save_undo(conn, kept)
    for old in dropped:
        _forget_undo(conn, old)


def _forget_undo(conn, entry):
    """An entry leaving the history for good: remove the old copies of replaced files.

    A file the professor replaced keeps its previous copy on disk for exactly as long
    as the apply that replaced it can be undone, and no longer. A copy some row still
    points at, because the undo already happened or the fetch never did, stays.
    """
    import os
    import db

    for op in entry.get("journal") or []:
        if op.get("op") != "mat~":
            continue
        for name in ((op.get("before") or {}).get("stored_name"),
                     (op.get("before") or {}).get("preview_name")):
            if not name:
                continue
            used = conn.execute("SELECT 1 FROM materials WHERE stored_name=? OR preview_name=?",
                                (name, name)).fetchone()
            if not used:
                try:
                    os.remove(os.path.join(db.UPLOAD_DIR, name))
                except OSError:
                    pass


def history(conn):
    """What can be undone, newest first, as the review shows it."""
    return [{"id": e["id"], "at": e["at"], "summary": e.get("summary") or "",
             "classes": e.get("classes") or []} for e in load_undo(conn)]


def _eq(a, b):
    """Two stored values the same, the way the page would say so."""
    if (a is None or a == "") and (b is None or b == ""):
        return True
    x, y = canvas._number(a), canvas._number(b)
    if x is not None and y is not None:
        return round(x, 4) == round(y, 4)
    return str(a if a is not None else "") == str(b if b is not None else "")


def _attached(conn, dependents, row_id):
    for table, column, why in dependents:
        if conn.execute("SELECT 1 FROM %s WHERE %s=? LIMIT 1" % (table, column),
                        (row_id,)).fetchone():
            return why
    return None


def undo_apply(conn, state, entry, extract_text=None):
    """Reverse one apply, keeping anything he has touched since. Does not commit.

    Returns (report, files_to_remove). Files are removed only after the caller has
    committed, so a failure part way through cannot leave rows pointing at nothing.
    """
    import os
    import db

    done = {"items": 0, "restored": 0, "files": 0, "categories": 0, "folders": 0}
    kept, remove = [], []

    for op in reversed(entry.get("journal") or []):
        kind, rid = op.get("op"), op.get("id")

        if kind == "mat+":
            row = conn.execute("SELECT * FROM materials WHERE id=?", (rid,)).fetchone()
            if not row:
                continue                                  # he already deleted it
            wrote = op.get("wrote") or {}
            title = _col(row, "title") or _col(row, "filename") or "A file"
            if _col(row, "class_id") != op.get("class") or \
                    not _eq(_col(row, "folder_id"), wrote.get("folder_id")) or \
                    not _eq(_col(row, "title"), wrote.get("title")):
                kept.append({"title": title, "why": "you moved or renamed it"})
                continue
            why = _attached(conn, MATERIAL_DEPENDENTS, rid)
            if why:
                kept.append({"title": title, "why": why})
                continue
            conn.execute("DELETE FROM materials WHERE id=?", (rid,))
            remove += [n for n in (_col(row, "stored_name"), _col(row, "preview_name")) if n]
            done["files"] += 1

        elif kind == "mat~":
            row = conn.execute("SELECT * FROM materials WHERE id=?", (rid,)).fetchone()
            if not row:
                continue
            old = op.get("before") or {}
            name = old.get("stored_name")
            if name and not os.path.exists(os.path.join(db.UPLOAD_DIR, name)):
                kept.append({"title": op.get("title") or "A file",
                             "why": "its earlier version is no longer stored"})
                continue
            text = None
            if name and extract_text:
                try:
                    text = extract_text(os.path.join(db.UPLOAD_DIR, name),
                                        _col(row, "filename") or name)
                except Exception:
                    text = None
            conn.execute("UPDATE materials SET kind=?, stored_name=?, preview_name=?, size=?,"
                         " url=?, extracted_text=?, preview_status=NULL WHERE id=?",
                         (old.get("kind") or "file", name, old.get("preview_name"),
                          old.get("size"), old.get("url"), text, rid))
            remove += [n for n in (_col(row, "stored_name"), _col(row, "preview_name"))
                       if n and n not in (name, old.get("preview_name"))]
            done["files"] += 1

        elif kind == "folder+":
            busy = conn.execute("SELECT 1 FROM materials WHERE folder_id=? LIMIT 1", (rid,)).fetchone() \
                or conn.execute("SELECT 1 FROM file_folders WHERE parent_id=? LIMIT 1", (rid,)).fetchone()
            if not busy:
                conn.execute("DELETE FROM file_folders WHERE id=?", (rid,))
                done["folders"] += 1

        elif kind == "item~":
            row = conn.execute("SELECT * FROM items WHERE id=?", (rid,)).fetchone()
            if not row:
                continue
            sets, values, moved_on = [], [], False
            for column, (was, wrote) in (op.get("fields") or {}).items():
                if _eq(_col(row, column), wrote):
                    sets.append(column + "=?")
                    values.append(was)
                elif column != "import_key":
                    moved_on = True
            if sets:
                conn.execute("UPDATE items SET " + ", ".join(sets) + " WHERE id=?",
                             tuple(values) + (rid,))
                done["restored"] += 1
            if moved_on:
                kept.append({"title": _col(row, "title") or op.get("title") or "An assignment",
                             "why": "you changed it again since"})

        elif kind == "item+":
            row = conn.execute("SELECT * FROM items WHERE id=?", (rid,)).fetchone()
            if not row:
                continue
            wrote = op.get("wrote") or {}
            title = _col(row, "title") or "An assignment"
            if any(not _eq(_col(row, c), wrote.get(c)) for c in ITEM_WROTE) or \
                    (canvas._number(_col(row, "focus_seconds")) or 0) > 0:
                kept.append({"title": title, "why": "you've worked on it since"})
                continue
            why = _attached(conn, ITEM_DEPENDENTS, rid)
            if why:
                kept.append({"title": title, "why": why})
                continue
            conn.execute("DELETE FROM items WHERE id=?", (rid,))
            done["items"] += 1

        elif kind == "item-":
            snap = op.get("row") or {}
            if not conn.execute("SELECT 1 FROM items WHERE id=?", (rid,)).fetchone():
                _reinsert(conn, "items", snap)
                done["restored"] += 1

        elif kind == "cat-":
            if not conn.execute("SELECT 1 FROM grade_categories WHERE id=?", (rid,)).fetchone():
                _reinsert(conn, "grade_categories", op.get("row") or {})

        elif kind == "cat~":
            row = conn.execute("SELECT * FROM grade_categories WHERE id=?", (rid,)).fetchone()
            after, old = op.get("after") or {}, op.get("before") or {}
            if row and _eq(_col(row, "weight"), after.get("weight")) and \
                    _eq(_col(row, "drop_lowest"), after.get("drop_lowest")):
                conn.execute("UPDATE grade_categories SET weight=?, drop_lowest=? WHERE id=?",
                             (old.get("weight"), old.get("drop_lowest") or 0, rid))

        elif kind == "cat+":
            if not conn.execute("SELECT 1 FROM items WHERE category_id=? LIMIT 1", (rid,)).fetchone():
                conn.execute("DELETE FROM grade_categories WHERE id=?", (rid,))
                done["categories"] += 1

    # The review's memory, back to how it was: what was synced is no longer "seen", so
    # anything taken back is offered again rather than treated as deleted, and a
    # "keep mine" made in this apply is forgotten.
    for cid, keys in (entry.get("seenAdded") or {}).items():
        forget(state, cid, keys)
    for cid, previous in (entry.get("rejectedPrev") or {}).items():
        rejected = state.setdefault("courses", {}).setdefault(cid, {}).setdefault("rejected", {})
        for rk, value in previous.items():
            if value is None:
                rejected.pop(rk, None)
            else:
                rejected[rk] = value

    return {"undone": done, "kept": kept}, remove


def undo_message(report):
    d = report.get("undone") or {}

    def n(count, one, many=None):
        return "%d %s" % (count, one if count == 1 else (many or one + "s"))
    parts = []
    if d.get("items"):
        parts.append("removed " + n(d["items"], "assignment"))
    if d.get("restored"):
        parts.append("put back " + n(d["restored"], "assignment") + " as they were")
    if d.get("files"):
        parts.append("took back " + n(d["files"], "file"))
    msg = ("Undone: " + ", ".join(parts) + ".") if parts else "Undone."
    kept = report.get("kept") or []
    if kept:
        names = "; ".join("%s (%s)" % (k["title"], k["why"]) for k in kept[:4])
        more = " and %d more" % (len(kept) - 4) if len(kept) > 4 else ""
        msg += " Left as they are, because they have your work in them: " + names + more + "."
    return msg


@bp.route("/api/canvas/undo", methods=["POST"])
def undo_route():
    import os
    import db
    body = request.get_json(force=True) or {}
    extract = current_app.config.get("EXTRACT_TEXT")
    conn = db.get_db()
    try:
        entries = load_undo(conn)
        entry = next((e for e in entries if e.get("id") == body.get("id")), None)
        if not entry:
            return jsonify({"error": "That change can no longer be undone."}), 404
        state = load_state(conn)
        try:
            report, remove = undo_apply(conn, state, entry, extract)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        save_state(conn, state)
        save_undo(conn, [e for e in entries if e.get("id") != entry["id"]])
        report["message"] = undo_message(report)
        report["review"] = review_summary(build_review(conn, state))
    finally:
        conn.close()
    for name in remove:
        try:
            os.remove(os.path.join(db.UPLOAD_DIR, name))
        except OSError:
            pass
    return jsonify(report)


# ===========================================================================
# duplicates: the same assignment under two names
#
# Found on vesta.study, 2026-09-22: SD 381 had every assignment twice. The syllabus
# import calls one "Reading quiz (Week 1)"; Canvas calls it "Week 1 Readings Quiz".
# Matching by exact title missed every pair, so each Canvas assignment arrived as new,
# the course showed done work as overdue, and the two copies sat in two sets of grade
# categories counting the same work twice.
#
# `looks_same` decides whether two titles are plausibly one assignment. It is only
# ever a suggestion: the review asks, and Saif's rule is that his row survives and is
# linked to Canvas, while Canvas's copy goes.
# ===========================================================================

# Words that say nothing about which assignment it is. "week" is here because the
# week *number* is what carries the meaning, and numbers are compared on their own.
_TWIN_STOP = {"the", "a", "an", "and", "or", "of", "on", "to", "in", "for", "your", "my",
              "with", "part", "activity", "activities", "module", "week", "due", "complete",
              "about", "this", "that", "is", "at", "by", "from"}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
             "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "final": None}
# Types too general to tell two assignments apart.
_LOOSE_TYPES = {"assignment", "other", "homework", "", None}


def _stem(word):
    if word.endswith("zzes"):
        return word[:-3]                  # quizzes -> quiz
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _title_parts(title):
    """(significant words, labelled numbers) for a title.

    A number is labelled by the word before it: "Week 2" is ("week", 2), "Quiz 03" is
    ("quiz", 3). The label is what makes numbers comparable. Canvas titles SD 381 as
    "MODULE 1, Week 2, short discussion on adulting" while the syllabus says "Short
    discussion (Week 2)": the 1 labels the module, the 2 the week, and only the week
    can be compared with the syllabus's week. Ordinals ("second discussion") count as
    words, so they have to match as words.
    """
    words, labelled = [], []
    prev = ""
    for tok in re.findall(r"[a-z]+|\d+", (title or "").lower()):
        if tok.isdigit():
            labelled.append((prev, int(tok)))
            prev = ""
            continue
        stem = _stem(tok)
        if tok not in _TWIN_STOP and len(tok) > 1:
            words.append(stem)
        prev = stem
    return words, labelled


def _numbers_by_label(labelled):
    out = {}
    for label, n in labelled:
        out.setdefault(label, set()).add(n)
    return out


def _days_apart(a, b):
    from datetime import date
    try:
        x, y = date.fromisoformat(str(a)[:10]), date.fromisoformat(str(b)[:10])
    except ValueError:
        return None
    return abs((x - y).days)


def looks_same(mine, theirs):
    """How strongly two assignments look like one, 0 for not at all.

    Each takes a dict of title, type and due_date. Three things rule a pair out:

    * a number labelling the same thing differently: Week 1 against Week 2, Quiz 01
      against Quiz 02. Module 1 against Week 2 is not a contradiction;
    * due dates more than a day apart;
    * too few shared words: the shorter title's words must mostly appear in the
      longer one. A title of one significant word ("Quiz 3", "Discussion 6") has to
      agree on a labelled number as well, since the word alone says nothing.
    """
    ta, tb = (mine.get("type") or ""), (theirs.get("type") or "")
    if ta != tb and ta not in _LOOSE_TYPES and tb not in _LOOSE_TYPES:
        return 0.0
    wa, la = _title_parts(mine.get("title"))
    wb, lb = _title_parts(theirs.get("title"))
    na, nb = _numbers_by_label(la), _numbers_by_label(lb)
    for label in set(na) & set(nb):
        if not (na[label] & nb[label]):
            return 0.0
    agreeing = sum(1 for label in set(na) & set(nb) if label)

    short, long_ = sorted((set(wa), set(wb)), key=len)
    if not short:
        return 0.0
    overlap = len(short & long_) / float(len(short))
    if len(short) >= 2:
        if overlap < 0.75:
            return 0.0
    elif overlap < 1.0 or not agreeing:
        return 0.0

    apart = _days_apart(mine.get("due_date"), theirs.get("due_date")) \
        if mine.get("due_date") and theirs.get("due_date") else None
    if apart is not None and apart > 1:
        return 0.0
    score = overlap + 0.1 * len(short & long_) + 0.2 * agreeing
    if apart == 0:
        score += 0.5
    elif apart == 1:
        score += 0.25
    return round(score, 4)


def find_twins(existing, items):
    """Mark each Canvas assignment that looks like one of his under another name.

    Two cases, and both end the same way when he says they are one assignment:

    * the Canvas assignment is new: nothing matched it exactly, but one of his rows
      looks like it. Without this it would arrive as a second copy.
    * Canvas's copy is already here, from an earlier sync that missed the match, and
      one of his rows looks like it. That is SD 381 as found: done work beside an
      overdue copy of itself.

    Only his rows are candidates: ones no Canvas assignment already claims. Pairs are
    taken best first, so each row and each assignment is in at most one.
    """
    rows = list(existing or [])
    by_id = {_col(r, "id"): r for r in rows}
    claimed = {i.get("existingId") for i in items if i.get("existingId")}
    mine = [r for r in rows if _col(r, "id") not in claimed
            and not str(_col(r, "import_key") or "").startswith("canvas:")]
    pairs = []
    for it in items:
        copy = None
        if it.get("existingId"):
            row = by_id.get(it["existingId"])
            if not row or _col(row, "import_key") != it.get("importKey"):
                continue        # matched one of his own rows by title: already one
            copy = row
        elif it.get("change") != "new":
            continue
        theirs = {"title": it.get("title"), "type": it.get("type"), "due_date": it.get("dueDate")}
        for r in mine:
            score = looks_same({"title": _col(r, "title"), "type": _col(r, "type"),
                                "due_date": _col(r, "due_date")}, theirs)
            if score > 0:
                pairs.append((score, it, r, copy))
    pairs.sort(key=lambda p: -p[0])
    taken_items, taken_rows = set(), set()
    for score, it, r, copy in pairs:
        if id(it) in taken_items or _col(r, "id") in taken_rows:
            continue
        taken_items.add(id(it))
        taken_rows.add(_col(r, "id"))
        it["twin"] = {"mineId": _col(r, "id"), "mineTitle": _col(r, "title"),
                      "mineDue": _col(r, "due_date"), "mineTime": _col(r, "due_time"),
                      "mineDone": (_col(r, "status") or "") == "done",
                      "copyId": _col(copy, "id") if copy else None, "score": score}
    return items


def _row_snapshot(row):
    """Every column of a row, for putting it back on undo. Not `user_id`: on Postgres
    the column's own default fills it, and a UUID object does not survive JSON."""
    return {k: row[k] for k in row.keys() if k != "user_id"}


def _reinsert(conn, table, snap):
    cols = list(snap.keys())
    conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(cols), ",".join("?" * len(cols))),
                 tuple(snap[c] for c in cols))


def _copy_untouched(conn, row):
    """Canvas's copy can go only if he never did anything with it."""
    if (_col(row, "status") or "todo") != "todo" or _col(row, "completed_at") or \
            (canvas._number(_col(row, "focus_seconds")) or 0) > 0:
        return "you've worked on Canvas's copy"
    return _attached(conn, ITEM_DEPENDENTS, _col(row, "id"))


def _merge_twin(conn, unit, journal, canvas_cat_names):
    """Same assignment: his row gains Canvas's link; Canvas's copy, if any, goes.

    A grade category Canvas's copy leaves empty goes with it, if it is one of
    Canvas's: Vesta counts every category's weight towards the course total whether
    or not anything is in it, so an emptied one would go on inflating the total.
    Returns a reason when it could not merge, or None.
    """
    t = unit["_item"]["twin"]
    mine = conn.execute("SELECT * FROM items WHERE id=?", (t["mineId"],)).fetchone()
    if not mine:
        return "your copy is gone"
    copy = conn.execute("SELECT * FROM items WHERE id=?", (t["copyId"],)).fetchone() \
        if t.get("copyId") else None
    if copy is not None:
        why = _copy_untouched(conn, copy)
        if why:
            return why
    conn.execute("UPDATE items SET import_key=? WHERE id=?", (unit["key"], t["mineId"]))
    journal.append({"op": "item~", "id": t["mineId"], "class": unit["classId"],
                    "title": _col(mine, "title"),
                    "fields": {"import_key": [_col(mine, "import_key"), unit["key"]]}})
    if copy is not None:
        journal.append({"op": "item-", "id": t["copyId"], "row": _row_snapshot(copy)})
        conn.execute("DELETE FROM items WHERE id=?", (t["copyId"],))
        cat_id = _col(copy, "category_id")
        if cat_id and not conn.execute("SELECT 1 FROM items WHERE category_id=? LIMIT 1",
                                       (cat_id,)).fetchone():
            cat = conn.execute("SELECT * FROM grade_categories WHERE id=?", (cat_id,)).fetchone()
            if cat and (_col(cat, "name") or "").lower() in canvas_cat_names:
                journal.append({"op": "cat-", "id": cat_id, "row": _row_snapshot(cat)})
                conn.execute("DELETE FROM grade_categories WHERE id=?", (cat_id,))
    return None


# ---------------------------------------------------------------------------
# grade categories that count the same work twice
# ---------------------------------------------------------------------------

def category_overlap(items, cats, plan):
    """His categories and Canvas's side by side, when together they pass 100%.

    Returns None when there is nothing to choose: only one set, or a total at or under
    100%. Canvas's are the ones named like a Canvas assignment group; the rest are his.
    The total is worked out exactly as the page's grade maths does it, every
    category's weight plus the weights of assignments in no category.
    """
    canvas_names = {(c.get("name") or "").lower() for c in (plan.get("categories") or [])}
    counts = {}
    for it in items:
        counts[_col(it, "category_id")] = counts.get(_col(it, "category_id"), 0) + 1
    cat_ids = {_col(c, "id") for c in cats}
    total = sum(canvas._number(_col(c, "weight")) or 0.0 for c in cats)
    total += sum(canvas._number(_col(it, "weight")) or 0.0 for it in items
                 if _col(it, "category_id") not in cat_ids)
    describe = lambda c: {"id": _col(c, "id"), "name": _col(c, "name"),
                          "weight": canvas._number(_col(c, "weight")),
                          "count": counts.get(_col(c, "id"), 0)}
    theirs = [describe(c) for c in cats if (_col(c, "name") or "").lower() in canvas_names]
    mine = [describe(c) for c in cats if (_col(c, "name") or "").lower() not in canvas_names]
    if not theirs or not mine or total <= 100.5:
        return None
    return {"mine": mine, "canvas": theirs, "total": round(total, 2)}


def _resolve_categories(conn, unit, use_canvas, snapshot, journal):
    """Make one set of categories stand. Returns the titles left outside any category.

    Keep mine: Canvas's categories go, and their assignments are left in none, because
    no category of his is known to cover them. Use Canvas's: his go, and each of his
    assignments linked to Canvas moves into the category its Canvas assignment is in
    (or takes its own weight, where Canvas gives it one alone); the rest are left in
    none. Either way the result names what was left out, so nothing drops out of the
    grade unnoticed.
    """
    class_id = unit["classId"]
    ov = unit["after"]
    going = ov["mine"] if use_canvas else ov["canvas"]
    going_ids = {c["id"] for c in going}
    plan = snapshot.get("plan") or {}
    by_key = {i.get("importKey"): i for i in (plan.get("items") or [])}
    canvas_cat = {}
    for pc in plan.get("categories") or []:
        row = conn.execute("SELECT id FROM grade_categories WHERE class_id=? AND lower(name)=lower(?)",
                           (class_id, pc.get("name") or "")).fetchone()
        if row:
            canvas_cat[pc.get("canvasId")] = _col(row, "id")
    loose = []
    for it in conn.execute("SELECT * FROM items WHERE class_id=?", (class_id,)).fetchall():
        if _col(it, "category_id") not in going_ids:
            continue
        new_cat, new_weight = None, _col(it, "weight")
        planned = by_key.get(_col(it, "import_key")) if use_canvas else None
        if planned:
            new_cat = canvas_cat.get(planned.get("categoryCanvasId"))
            if not new_cat and planned.get("weight") is not None and plan.get("weightsFromCanvas"):
                new_weight = planned.get("weight")
        if not new_cat and new_weight is None:
            loose.append(_col(it, "title") or "An assignment")
        conn.execute("UPDATE items SET category_id=?, weight=? WHERE id=?",
                     (new_cat, new_weight, _col(it, "id")))
        journal.append({"op": "item~", "id": _col(it, "id"), "class": class_id,
                        "title": _col(it, "title"),
                        "fields": {"category_id": [_col(it, "category_id"), new_cat],
                                   "weight": [_col(it, "weight"), new_weight]}})
    for c in going:
        row = conn.execute("SELECT * FROM grade_categories WHERE id=?", (c["id"],)).fetchone()
        if row:
            journal.append({"op": "cat-", "id": c["id"], "row": _row_snapshot(row)})
            conn.execute("DELETE FROM grade_categories WHERE id=?", (c["id"],))
    return loose

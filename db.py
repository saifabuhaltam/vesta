import os
import sqlite3
import uuid

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "vesta.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS classes (
    id TEXT PRIMARY KEY,
    code TEXT,
    name TEXT,
    professor TEXT,
    color TEXT,
    notes TEXT,
    grade_scale TEXT,
    website TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS schedule_entries (
    id TEXT PRIMARY KEY,
    class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    day INTEGER,
    start TEXT,
    end TEXT,
    location TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    class_id TEXT REFERENCES classes(id) ON DELETE CASCADE,
    title TEXT,
    type TEXT,
    due_date TEXT,
    due_time TEXT,
    status TEXT,
    completed_at TEXT,
    weight REAL,
    score REAL,
    notes TEXT,
    focus_seconds INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS subtasks (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    title TEXT,
    done INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY,
    -- nullable, for the same reason as notes: upload first, file it later
    class_id TEXT REFERENCES classes(id) ON DELETE CASCADE,
    category TEXT,
    title TEXT,
    kind TEXT,
    url TEXT,
    filename TEXT,
    stored_name TEXT,
    mimetype TEXT,
    size INTEGER,
    extracted_text TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    -- nullable: a note can be jotted down before there is anywhere to file it
    class_id TEXT REFERENCES classes(id) ON DELETE CASCADE,
    title TEXT,
    folder_id TEXT,
    text TEXT,
    linked_item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
    updated_at TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS note_folders (
    id TEXT PRIMARY KEY,
    class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES note_folders(id) ON DELETE CASCADE,
    name TEXT,
    kind TEXT DEFAULT 'custom',
    sort_order INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS note_versions (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    title TEXT,
    text TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS note_links (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    item_id TEXT REFERENCES items(id) ON DELETE CASCADE,
    file_id TEXT REFERENCES materials(id) ON DELETE CASCADE,
    event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
    schedule_entry_id TEXT REFERENCES schedule_entries(id) ON DELETE CASCADE,
    headstart_id TEXT REFERENCES headstarts(id) ON DELETE CASCADE,
    target_note_id TEXT REFERENCES notes(id) ON DELETE CASCADE,
    label TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS syllabus_topics (
    id TEXT PRIMARY KEY,
    class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    title TEXT,
    done INTEGER DEFAULT 0,
    sort_order INTEGER
);

CREATE TABLE IF NOT EXISTS headstarts (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    content TEXT,
    status TEXT DEFAULT 'draft',
    instructions TEXT,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(item_id, kind)
);

CREATE TABLE IF NOT EXISTS rubrics (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
    criteria TEXT,
    total_points REAL,
    created_at TEXT,
    UNIQUE(material_id)
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    class_id TEXT REFERENCES classes(id) ON DELETE CASCADE,
    title TEXT,
    kind TEXT,
    date TEXT,
    start TEXT,
    end TEXT,
    all_day INTEGER DEFAULT 0,
    location TEXT,
    notes TEXT,
    created_at TEXT
);

-- Which specific materials a Headstart run was given. Without this a generation
-- is a black box: you cannot see what it read, or repeat it with the same inputs.
CREATE TABLE IF NOT EXISTS headstart_sources (
    id TEXT PRIMARY KEY,
    headstart_id TEXT REFERENCES headstarts(id) ON DELETE CASCADE,
    material_id TEXT REFERENCES materials(id) ON DELETE CASCADE,
    note_id TEXT REFERENCES notes(id) ON DELETE CASCADE,
    folder_id TEXT REFERENCES note_folders(id) ON DELETE CASCADE,
    syllabus_id TEXT REFERENCES syllabus_topics(id) ON DELETE CASCADE,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS flashcard_decks (
    id TEXT PRIMARY KEY,
    class_id TEXT REFERENCES classes(id) ON DELETE CASCADE,
    name TEXT,
    description TEXT,
    source_note_id TEXT REFERENCES notes(id) ON DELETE SET NULL,
    source_material_id TEXT REFERENCES materials(id) ON DELETE SET NULL,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS flashcards (
    id TEXT PRIMARY KEY,
    deck_id TEXT NOT NULL REFERENCES flashcard_decks(id) ON DELETE CASCADE,
    front TEXT,
    back TEXT,
    kind TEXT DEFAULT 'term',
    ease REAL DEFAULT 2.5,
    interval_days INTEGER DEFAULT 0,
    repetitions INTEGER DEFAULT 0,
    lapses INTEGER DEFAULT 0,
    due_date TEXT,
    last_reviewed_at TEXT,
    suspended INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS quizzes (
    id TEXT PRIMARY KEY,
    class_id TEXT REFERENCES classes(id) ON DELETE CASCADE,
    item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
    title TEXT,
    kind TEXT DEFAULT 'quiz',
    difficulty TEXT DEFAULT 'mixed',
    source TEXT DEFAULT 'manual',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS quiz_questions (
    id TEXT PRIMARY KEY,
    quiz_id TEXT NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    kind TEXT DEFAULT 'multiple_choice',
    prompt TEXT,
    choices TEXT,
    answer TEXT,
    explanation TEXT,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id TEXT PRIMARY KEY,
    quiz_id TEXT NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    responses TEXT,
    score REAL,
    correct_count INTEGER DEFAULT 0,
    total_count INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT
);

-- Small key/value store for app-level settings that are not per-record.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- One row per AI call, so the daily cap and the spend picture are real numbers
-- rather than a guess.
CREATE TABLE IF NOT EXISTS ai_usage (
    id TEXT PRIMARY KEY,
    kind TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    day TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS term_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT,
    start_date TEXT,
    end_date TEXT
);
"""


# One app, two databases. Locally it is SQLite in DATA_DIR, which needs no server and
# no setup. Deployed, DATABASE_URL points at Postgres and `pgshim` presents the same
# sqlite3-shaped interface over it, so not one of the app's ~200 get_db() calls or 262
# queries has to change.
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip() or None


def current_user_id():
    """Whose data this request may touch, or None outside a request.

    Read from Flask's request context rather than passed in, so every existing
    `get_db()` call picks up the signed-in user without being edited. On Postgres this
    becomes the JWT claim that Row Level Security filters on; on SQLite it is unused,
    because a local install has exactly one user.
    """
    try:
        from flask import g, has_request_context
        return getattr(g, "user_id", None) if has_request_context() else None
    except Exception:
        return None


def get_db(user_id=None):
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row

        import pgshim
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        return pgshim.Connection(conn, user_id or current_user_id())
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


PG_SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "cloud", "migrate", "pg_schema.sql")
# Arbitrary, and only has to agree between workers of the same app.
SCHEMA_LOCK_ID = 827419901


def ensure_pg_schema():
    """Create the tables the first time the deployed app boots.

    This exists so that deploying is a matter of setting variables and nothing else:
    no pasting 570 lines of SQL into a web console, and no psql on the machine doing
    the deploying.

    Guarded by a Postgres advisory lock, because gunicorn starts several workers at
    once and they would otherwise race to create the same tables. Every statement in
    the file is `create ... if not exists`, so running it twice is harmless; the lock
    is about not running it twice at the same instant.
    """
    import psycopg
    if not os.path.exists(PG_SCHEMA_FILE):
        raise RuntimeError(
            f"DATABASE_URL is set but {PG_SCHEMA_FILE} is missing, so the tables "
            "cannot be created. Run cloud/migrate/gen_pg_schema.py and commit it.")
    sql = open(PG_SCHEMA_FILE, encoding="utf-8").read().split("-- Every row below")[0]
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
            try:
                cur.execute("select to_regclass('public.classes')")
                if cur.fetchone()[0] is not None:
                    return                 # already built by an earlier boot
                cur.execute(sql)
            finally:
                cur.execute("select pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))


def init_db():
    # On Postgres the tables build themselves on first boot. The ALTER-by-ALTER
    # migration below is a SQLite story: it exists because a local database predates
    # most of these columns.
    if DATABASE_URL:
        ensure_pg_schema()
        return
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO term_settings (id, name, start_date, end_date) VALUES (1, '', '', '')"
        " ON CONFLICT DO NOTHING"
    )
    # migrate: add grade_scale to classes if this db predates it
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(classes)").fetchall()]
    if "grade_scale" not in cols:
        conn.execute("ALTER TABLE classes ADD COLUMN grade_scale TEXT")
    if "website" not in cols:
        conn.execute("ALTER TABLE classes ADD COLUMN website TEXT")
    # note folders became a tree with typed folders
    fcols = [r["name"] for r in conn.execute("PRAGMA table_info(note_folders)").fetchall()]
    if "parent_id" not in fcols:
        conn.execute("ALTER TABLE note_folders ADD COLUMN parent_id TEXT")
    if "kind" not in fcols:
        conn.execute("ALTER TABLE note_folders ADD COLUMN kind TEXT DEFAULT 'custom'")
    if "sort_order" not in fcols:
        conn.execute("ALTER TABLE note_folders ADD COLUMN sort_order INTEGER DEFAULT 0")

    ncols = [r["name"] for r in conn.execute("PRAGMA table_info(notes)").fetchall()]
    if "pinned" not in ncols:
        conn.execute("ALTER TABLE notes ADD COLUMN pinned INTEGER DEFAULT 0")
    if "starred" not in ncols:
        conn.execute("ALTER TABLE notes ADD COLUMN starred INTEGER DEFAULT 0")
    if "sort_order" not in ncols:
        conn.execute("ALTER TABLE notes ADD COLUMN sort_order INTEGER DEFAULT 0")
    if "deleted_at" not in ncols:
        conn.execute("ALTER TABLE notes ADD COLUMN deleted_at TEXT")
    if "title" not in ncols:
        conn.execute("ALTER TABLE notes ADD COLUMN title TEXT")
    if "folder_id" not in ncols:
        conn.execute("ALTER TABLE notes ADD COLUMN folder_id TEXT")
    if "updated_at" not in ncols:
        conn.execute("ALTER TABLE notes ADD COLUMN updated_at TEXT")
    mcols = [r["name"] for r in conn.execute("PRAGMA table_info(materials)").fetchall()]
    if "item_id" not in mcols:
        conn.execute("ALTER TABLE materials ADD COLUMN item_id TEXT")

    lcols = [r["name"] for r in conn.execute("PRAGMA table_info(note_links)").fetchall()]
    if lcols and "headstart_id" not in lcols:
        conn.execute("ALTER TABLE note_links ADD COLUMN headstart_id TEXT")

    # Syllabus import. A meeting has a kind (lecture, lab, tutorial) and the dates it
    # runs; an item can belong to a grade category; each import is kept for review.
    scols = [r["name"] for r in conn.execute("PRAGMA table_info(schedule_entries)").fetchall()]
    for col, ddl in (("kind", "TEXT DEFAULT 'lecture'"), ("section", "TEXT"),
                     ("start_date", "TEXT"), ("end_date", "TEXT")):
        if col not in scols:
            conn.execute(f"ALTER TABLE schedule_entries ADD COLUMN {col} {ddl}")
    icols = [r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    for col in ("category_id", "import_key", "location"):
        if col not in icols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS grade_categories (
            id TEXT PRIMARY KEY,
            class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
            name TEXT,
            weight REAL,
            drop_lowest INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS syllabus_imports (
            id TEXT PRIMARY KEY,
            class_id TEXT REFERENCES classes(id) ON DELETE SET NULL,
            filename TEXT,
            stored_name TEXT,
            mimetype TEXT,
            draft TEXT,
            official TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            status TEXT DEFAULT 'review',
            created_at TEXT,
            imported_at TEXT
        )""")

    # Calendar sync. An account is one connected calendar: a Google calendar, or a
    # read-only .ics feed such as Canvas. `sync_links` maps any local object to its
    # counterpart in that account and holds a fingerprint of each side as it stood at
    # the last successful sync, which is what tells a one-sided edit apart from a real
    # conflict: both hashes moved since `last_synced` means both ends changed.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_accounts (
            id TEXT PRIMARY KEY,
            provider TEXT,                    -- 'google' | 'ics'
            name TEXT,
            url TEXT,                         -- ics feeds
            calendar_id TEXT,                 -- google: the calendar Vesta writes to
            refresh_token TEXT,
            access_token TEXT,
            token_expires TEXT,
            direction TEXT DEFAULT 'both',    -- 'pull' | 'push' | 'both'
            enabled INTEGER DEFAULT 1,
            sync_token TEXT,                  -- google incremental sync
            last_sync TEXT,
            last_error TEXT,
            created_at TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_links (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES calendar_accounts(id) ON DELETE CASCADE,
            local_kind TEXT,                  -- 'item' | 'event' | 'meeting'
            local_id TEXT,
            external_id TEXT,
            etag TEXT,
            local_hash TEXT,                  -- what each side looked like when
            remote_hash TEXT,                 -- they last agreed
            remote_updated TEXT,
            last_synced TEXT,
            state TEXT DEFAULT 'clean',       -- 'clean' | 'conflict' | 'remote_ahead'
            UNIQUE(account_id, local_kind, local_id),
            UNIQUE(account_id, external_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS sync_links_by_local ON sync_links(local_kind, local_id)")

    # An incoming feed is reviewed before it lands, exactly like a syllabus.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_imports (
            id TEXT PRIMARY KEY,
            account_id TEXT REFERENCES calendar_accounts(id) ON DELETE SET NULL,
            class_id TEXT REFERENCES classes(id) ON DELETE CASCADE,
            source TEXT,                      -- 'ics' | 'sfu' | 'google'
            label TEXT,
            draft TEXT,
            status TEXT DEFAULT 'review',
            created_at TEXT,
            imported_at TEXT
        )""")
    # the class used to live only inside the draft JSON, which left no way to find and
    # replace a class's previous unapplied draft
    cicols = [r["name"] for r in conn.execute("PRAGMA table_info(calendar_imports)").fetchall()]
    if "class_id" not in cicols:
        conn.execute("ALTER TABLE calendar_imports ADD COLUMN class_id TEXT")

    # Where an event came from, so one that synced in is never mistaken for one the
    # student typed, and a remote deletion can be carried without losing the row.
    ecols = [r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    for col, ddl in (("source", "TEXT DEFAULT 'vesta'"), ("account_id", "TEXT"),
                     ("external_id", "TEXT"), ("read_only", "INTEGER DEFAULT 0"),
                     ("updated_at", "TEXT"), ("deleted_at", "TEXT")):
        if col not in ecols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")

    conn.commit()
    drop_class_not_null(conn)
    move_item_links(conn)
    conn.close()


def move_item_links(conn):
    """One file, many assignments.

    A file used to carry a single `item_id`. Attachments now live in `item_files`, so
    the same rubric or reading can belong to several assignments while being stored
    once. Any `item_id` still set is copied across and then cleared, which makes this
    safe to run on every start: detaching deletes the link row, and nothing brings
    it back.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS item_files (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
            created_at TEXT,
            UNIQUE(item_id, material_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS item_files_by_material ON item_files(material_id)")
    rows = conn.execute(
        "SELECT id, item_id, created_at FROM materials "
        "WHERE item_id IS NOT NULL AND item_id IN (SELECT id FROM items)").fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO item_files (id, item_id, material_id, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT DO NOTHING",
            (str(uuid.uuid4()), r["item_id"], r["id"], r["created_at"]))
    conn.execute("UPDATE materials SET item_id=NULL WHERE item_id IS NOT NULL")
    conn.commit()


def drop_class_not_null(conn):
    """Let a note or a file exist before it has been filed under a class.

    Both columns were created NOT NULL. SQLite cannot relax that in place, so the
    table is rebuilt: new table, copy, drop, rename. Foreign keys are turned off for
    the swap, because dropping the old table would otherwise cascade into the rows
    that were just copied across.
    """
    for table, create in (("notes", NOTES_NULLABLE), ("materials", MATERIALS_NULLABLE)):
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not info:
            continue
        col = [r for r in info if r["name"] == "class_id"]
        if not col or not col[0]["notnull"]:
            continue                      # already nullable, nothing to do

        names = [r["name"] for r in info]
        cols = ", ".join(names)
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN")
            conn.execute(create.format(tmp=f"{table}__new"))
            conn.execute(f"INSERT INTO {table}__new ({cols}) SELECT {cols} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


NOTES_NULLABLE = """
CREATE TABLE {tmp} (
    id TEXT PRIMARY KEY,
    class_id TEXT REFERENCES classes(id) ON DELETE CASCADE,
    title TEXT,
    folder_id TEXT,
    text TEXT,
    linked_item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
    updated_at TEXT,
    created_at TEXT,
    pinned INTEGER DEFAULT 0,
    starred INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    deleted_at TEXT
)
"""

MATERIALS_NULLABLE = """
CREATE TABLE {tmp} (
    id TEXT PRIMARY KEY,
    class_id TEXT REFERENCES classes(id) ON DELETE CASCADE,
    category TEXT,
    title TEXT,
    kind TEXT,
    url TEXT,
    filename TEXT,
    stored_name TEXT,
    mimetype TEXT,
    size INTEGER,
    extracted_text TEXT,
    created_at TEXT,
    item_id TEXT
)
"""

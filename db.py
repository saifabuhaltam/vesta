import os
import sqlite3
import uuid
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "vesta.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)

SCHEMA = """
-- A term: "Fall 2026". Everything academic hangs off a semester through its class,
-- which is what lets a finished term be archived whole and still be read later.
CREATE TABLE IF NOT EXISTS semesters (
    id TEXT PRIMARY KEY,
    name TEXT,
    start_date TEXT,
    end_date TEXT,
    -- 'active' | 'archived'. Archiving hides a term from the current workspace
    -- without touching a row of its data.
    status TEXT DEFAULT 'active',
    archived_at TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS classes (
    id TEXT PRIMARY KEY,
    -- the term this class was taken in. Deleting a semester is refused while it
    -- still has classes, so in practice this is never null after the migration.
    semester_id TEXT REFERENCES semesters(id) ON DELETE SET NULL,
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
    -- which term this belongs to; mirrors the class's when there is one
    semester_id TEXT REFERENCES semesters(id) ON DELETE SET NULL,
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
    -- which term this belongs to; mirrors the class's when there is one
    semester_id TEXT REFERENCES semesters(id) ON DELETE SET NULL,
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
    -- Office files are converted to PDF once so they can be previewed in the browser.
    -- preview_status is one of pending, ready or failed; null means never attempted.
    preview_name TEXT,
    preview_status TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    -- which term this belongs to; mirrors the class's when there is one
    semester_id TEXT REFERENCES semesters(id) ON DELETE SET NULL,
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

-- Files get the same folder tree notes have. Before this a file carried a single
-- flat `category` string guessed from its filename, which could not be renamed,
-- nested or added to. `kind` marks the folders created for every new class so they
-- can be recognised later; a folder the user makes is 'custom'.
CREATE TABLE IF NOT EXISTS file_folders (
    id TEXT PRIMARY KEY,
    class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES file_folders(id) ON DELETE CASCADE,
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
    -- which term this belongs to; mirrors the class's when there is one
    semester_id TEXT REFERENCES semesters(id) ON DELETE SET NULL,
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
    -- which term this belongs to; mirrors the class's when there is one
    semester_id TEXT REFERENCES semesters(id) ON DELETE SET NULL,
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
    -- which term this belongs to; mirrors the class's when there is one
    semester_id TEXT REFERENCES semesters(id) ON DELETE SET NULL,
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
PG_MIGRATIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "cloud", "migrate", "pg_migrations.sql")
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


def parse_migrations(text):
    """Split the migrations file into (id, sql) pairs on its `-- migration:` lines."""
    blocks, mid, body = [], None, []
    for line in text.splitlines():
        if line.startswith("-- migration:"):
            if mid:
                blocks.append((mid, "\n".join(body)))
            mid, body = line.split(":", 1)[1].strip(), []
        elif mid:
            body.append(line)
    if mid:
        blocks.append((mid, "\n".join(body)))
    return [(i, sql) for i, sql in blocks if sql.strip()]


def run_pg_migrations():
    """Apply anything in pg_migrations.sql this database has not seen.

    `ensure_pg_schema` builds the tables once and then returns early forever after,
    so without this a deployed database could never gain a column. Each block is
    applied in its own transaction and recorded, which is what makes a redeploy a
    no-op rather than a re-run.
    """
    import psycopg
    if not os.path.exists(PG_MIGRATIONS_FILE):
        return []
    blocks = parse_migrations(open(PG_MIGRATIONS_FILE, encoding="utf-8").read())
    applied = []
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # Same lock as the schema build: gunicorn starts several workers at once
            # and they would otherwise apply the same migration side by side.
            cur.execute("select pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
            try:
                cur.execute("create table if not exists schema_migrations ("
                            " id text primary key,"
                            " applied_at timestamptz not null default now())")
                conn.commit()
                cur.execute("select id from schema_migrations")
                done = {r[0] for r in cur.fetchall()}
                for mid, sql in blocks:
                    if mid in done:
                        continue
                    cur.execute(sql)
                    cur.execute("insert into schema_migrations (id) values (%s)", (mid,))
                    conn.commit()
                    applied.append(mid)
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.execute("select pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))
                conn.commit()
    return applied


def init_db():
    # On Postgres the tables build themselves on first boot. The ALTER-by-ALTER
    # migration below is a SQLite story: it exists because a local database predates
    # most of these columns.
    if DATABASE_URL:
        ensure_pg_schema()
        run_pg_migrations()
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

    # Which of the student's Google calendars Vesta reads.
    #
    # Google issues its incremental sync token per calendar, not per account, so the
    # token has to live here rather than on calendar_accounts: one shared token would
    # mean every calendar after the first did a full pass on every sync.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_feeds (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES calendar_accounts(id) ON DELETE CASCADE,
            calendar_id TEXT NOT NULL,        -- Google's id for the calendar
            name TEXT,
            colour TEXT,
            writable INTEGER DEFAULT 0,       -- accessRole owner/writer
            is_vesta INTEGER DEFAULT 0,       -- the calendar Vesta itself writes to
            enabled INTEGER DEFAULT 0,        -- chosen by the student
            sync_token TEXT,
            last_sync TEXT,
            last_error TEXT,
            created_at TEXT,
            UNIQUE(account_id, calendar_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS calendar_feeds_by_account ON calendar_feeds(account_id)")

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
                     ("updated_at", "TEXT"), ("deleted_at", "TEXT"),
                     # which chosen calendar mirrored this in, so unticking one can
                     # withdraw exactly its events and nobody else's
                     ("feed_id", "TEXT")):
        if col not in ecols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")

    conn.commit()
    drop_class_not_null(conn)
    move_item_links(conn)
    # Last, deliberately: drop_class_not_null rebuilds notes and materials from a
    # fixed column list, so it has to run before those tables grow a new column.
    migrate_semesters(conn)
    migrate_file_folders(conn)
    conn.close()


# Everything that can exist without a class, and therefore cannot find its term by
# looking at one. Rows that do have a class mirror that class's semester.
SEMESTER_SCOPED = ("items", "events", "notes", "materials", "flashcard_decks", "quizzes")


def default_term_name(today=None):
    """"Fall 2026" from a date. Fall starts in September, Spring in January,
    Summer in May, which is the SFU calendar and close enough everywhere else."""
    import datetime
    d = today or datetime.date.today()
    season = "Spring" if d.month < 5 else ("Summer" if d.month < 9 else "Fall")
    return f"{season} {d.year}"


def migrate_semesters(conn):
    """Turn the single `term_settings` row into a real list of semesters.

    Before this, Vesta had one term forever: `term_settings` was pinned to id = 1 and
    no class referenced it, so a new semester meant deleting last term's work or
    living with it on the dashboard. Everything academic hangs off `classes`, so a
    `semester_id` there scopes assignments, files, notes and grades for free; the six
    tables in SEMESTER_SCOPED get their own copy because each of them can exist with
    no class at all.

    Safe to run on every start. The backfill only ever touches rows whose semester is
    still null, so a row that has been moved to another term stays where it was put.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS semesters (
            id TEXT PRIMARY KEY,
            name TEXT,
            start_date TEXT,
            end_date TEXT,
            status TEXT DEFAULT 'active',
            archived_at TEXT,
            created_at TEXT
        )""")
    for table in ("classes",) + SEMESTER_SCOPED:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if cols and "semester_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN semester_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS classes_by_semester ON classes(semester_id)")
    for table in SEMESTER_SCOPED:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_by_semester ON {table}(semester_id)")
    conn.commit()

    # The first semester is the term that was already there. Its name comes from
    # whatever was typed into term settings; an empty one gets today's term rather
    # than "Untitled", so the switcher reads like a calendar from the first boot.
    if not conn.execute("SELECT id FROM semesters LIMIT 1").fetchone():
        term = conn.execute("SELECT * FROM term_settings WHERE id=1").fetchone()
        now = _now()
        conn.execute(
            "INSERT INTO semesters (id, name, start_date, end_date, status, created_at)"
            " VALUES (?,?,?,?, 'active', ?)",
            (str(uuid.uuid4()),
             ((term["name"] if term else "") or "").strip() or default_term_name(),
             (term["start_date"] if term else "") or "",
             (term["end_date"] if term else "") or "", now))
        conn.commit()

    fallback = conn.execute(
        "SELECT id FROM semesters WHERE status='active' ORDER BY created_at LIMIT 1").fetchone()
    if not fallback:
        return
    sid = fallback["id"]
    conn.execute("UPDATE classes SET semester_id=? WHERE semester_id IS NULL", (sid,))
    for table in SEMESTER_SCOPED:
        # a row with a class belongs wherever that class belongs
        conn.execute(
            f"UPDATE {table} SET semester_id="
            f" (SELECT c.semester_id FROM classes c WHERE c.id={table}.class_id)"
            f" WHERE semester_id IS NULL AND class_id IS NOT NULL")
        # and one without a class falls back to the term that was current
        conn.execute(f"UPDATE {table} SET semester_id=? WHERE semester_id IS NULL", (sid,))
    conn.commit()


def _now():
    import datetime
    return datetime.datetime.utcnow().isoformat()


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
    preview_name TEXT,
    preview_status TEXT,
    folder_id TEXT,
    created_at TEXT,
    item_id TEXT
)
"""


# ---------------------------------------------------------------------------
# Which semester the app is looking at
# ---------------------------------------------------------------------------
# Held server-side rather than in the browser, for two reasons: every device sees the
# same term, and a request cannot ask for someone else's by passing an id, because the
# pointer itself lives in the signed-in user's own row.

ACTIVE_KEY = "active_semester"

# Newest first: the term that started most recently, falling back to when the row was
# made for a semester with no dates typed in yet.
SEMESTER_ORDER = "COALESCE(NULLIF(start_date,''), created_at) DESC, created_at DESC"


def ensure_semester(conn):
    """Guarantee this user has at least one semester, and return it.

    A new account on Postgres never runs the SQLite migration, so it arrives with no
    semesters at all. Creating one lazily here means sign-up needs no special case and
    the first class has somewhere to go.
    """
    row = conn.execute(
        f"SELECT * FROM semesters ORDER BY {SEMESTER_ORDER} LIMIT 1").fetchone()
    if row:
        return row
    sid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO semesters (id, name, start_date, end_date, status, created_at)"
        " VALUES (?,?,'','','active',?)", (sid, default_term_name(), _now()))
    conn.commit()
    return conn.execute("SELECT * FROM semesters WHERE id=?", (sid,)).fetchone()


def active_semester(conn):
    """The semester every scoped query filters on. Never None."""
    row = conn.execute(
        f"SELECT value FROM app_settings WHERE key='{ACTIVE_KEY}'").fetchone()
    sid = (row["value"] if row else None) or ""
    if sid:
        found = conn.execute("SELECT * FROM semesters WHERE id=?", (sid,)).fetchone()
        if found:
            return found
    # The pointer is missing or names a semester that has since been deleted. Fall
    # back to the newest one rather than showing an empty app.
    return ensure_semester(conn)


def active_semester_id(conn):
    return active_semester(conn)["id"]


def set_active_semester(conn, sid):
    # Not ON CONFLICT: app_settings is keyed (user_id, key) on Postgres and (key) on
    # SQLite, so the one spelling that works on both is update-then-insert.
    if not conn.execute("UPDATE app_settings SET value=? WHERE key=?",
                        (sid, ACTIVE_KEY)).rowcount:
        conn.execute("INSERT INTO app_settings (key, value) VALUES (?,?)",
                     (ACTIVE_KEY, sid))
    conn.commit()


def semester_for(conn, class_id=None):
    """The term a new row belongs to.

    A row with a class belongs wherever that class belongs, which keeps the copy on
    the row honest even if a class is ever moved between terms. Anything class-less
    lands in the term the student is currently looking at.
    """
    if class_id:
        row = conn.execute("SELECT semester_id FROM classes WHERE id=?", (class_id,)).fetchone()
        if row and row["semester_id"]:
            return row["semester_id"]
    return active_semester_id(conn)


# ---------------- file folders ----------------

# The folders every class starts with. Saif's list, in the order they appear in the
# sidebar. A class only gets "Projects" if it already has a file that was filed under
# the old `projects` category, so nobody grows a folder they never asked for.
DEFAULT_FILE_FOLDERS = (
    ("lectures", "Lectures"),
    ("readings", "Readings"),
    ("assignments", "Assignments"),
    ("rubrics", "Rubrics"),
    ("exams", "Exams"),
    ("syllabus", "Syllabus"),
    ("personal", "Personal"),
)

# The flat category each default folder replaces. `other` is deliberately absent:
# a file with no real category belongs at the top of its class, not in a bin called
# Other that nobody opens.
CATEGORY_TO_FOLDER_KIND = {
    "slides": "lectures",
    "readings": "readings",
    "rubrics": "rubrics",
    "exams": "exams",
    "syllabus": "syllabus",
    "personal": "personal",
    "projects": "projects",
}


def ensure_default_file_folders(conn, class_id):
    """Give a class its starting folders. Safe to call repeatedly.

    Matching is on `kind`, not name, so a folder the student renamed is still
    recognised and is not recreated under its original name.
    """
    have = {r["kind"] for r in conn.execute(
        "SELECT kind FROM file_folders WHERE class_id=? AND parent_id IS NULL", (class_id,)).fetchall()}
    now = datetime.utcnow().isoformat()
    for order, (kind, name) in enumerate(DEFAULT_FILE_FOLDERS):
        if kind in have:
            continue
        conn.execute(
            "INSERT INTO file_folders (id, class_id, parent_id, name, kind, sort_order, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), class_id, None, name, kind, order, now))


def folder_id_for_kind(conn, class_id, kind, name=None):
    """The class's folder of this kind, created on demand if it is missing."""
    row = conn.execute(
        "SELECT id FROM file_folders WHERE class_id=? AND kind=? AND parent_id IS NULL",
        (class_id, kind)).fetchone()
    if row:
        return row["id"]
    fid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO file_folders (id, class_id, parent_id, name, kind, sort_order, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (fid, class_id, None, name or kind.title(), kind, len(DEFAULT_FILE_FOLDERS), datetime.utcnow().isoformat()))
    return fid


def migrate_file_folders(conn):
    """Turn `materials.category` into a real folder tree.

    Runs on every start and only ever touches materials whose `folder_id` is still
    null, so a file that has since been moved by hand stays where it was put. The old
    `category` column is left alone: it still carries the filename guess made at upload
    time, and dropping a column means rebuilding the table for no gain.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_folders (
            id TEXT PRIMARY KEY,
            class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
            parent_id TEXT REFERENCES file_folders(id) ON DELETE CASCADE,
            name TEXT,
            kind TEXT DEFAULT 'custom',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS file_folders_by_class ON file_folders(class_id)")

    mcols = [r["name"] for r in conn.execute("PRAGMA table_info(materials)").fetchall()]
    if "folder_id" not in mcols:
        conn.execute("ALTER TABLE materials ADD COLUMN folder_id TEXT")
    # Converted Office previews. Here rather than in the earlier migration for the
    # same reason as folder_id: drop_class_not_null rebuilds materials from a fixed
    # column list and runs before this.
    if "preview_name" not in mcols:
        conn.execute("ALTER TABLE materials ADD COLUMN preview_name TEXT")
    if "preview_status" not in mcols:
        conn.execute("ALTER TABLE materials ADD COLUMN preview_status TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS materials_by_folder ON materials(folder_id)")
    conn.commit()

    for c in conn.execute("SELECT id FROM classes").fetchall():
        ensure_default_file_folders(conn, c["id"])
    conn.commit()

    # Backfill. One pass per category rather than per file: at any realistic library
    # size this is a handful of statements instead of a few hundred.
    rows = conn.execute(
        "SELECT DISTINCT class_id, category FROM materials"
        " WHERE folder_id IS NULL AND class_id IS NOT NULL AND category IS NOT NULL").fetchall()
    for r in rows:
        kind = CATEGORY_TO_FOLDER_KIND.get(r["category"])
        if not kind:
            continue
        fid = folder_id_for_kind(conn, r["class_id"], kind,
                                 "Projects" if kind == "projects" else None)
        conn.execute(
            "UPDATE materials SET folder_id=? WHERE folder_id IS NULL AND class_id=? AND category=?",
            (fid, r["class_id"], r["category"]))
    conn.commit()


def folder_descendants(conn, folder_id):
    """A folder and every folder beneath it, so a view can show a whole branch."""
    out, frontier = [folder_id], [folder_id]
    while frontier:
        rows = conn.execute(
            "SELECT id FROM file_folders WHERE parent_id IN ({})".format(
                ",".join("?" * len(frontier))), frontier).fetchall()
        frontier = [r["id"] for r in rows]
        out.extend(frontier)
    return out

import os
import sqlite3

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "termboard.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS classes (
    id TEXT PRIMARY KEY,
    code TEXT,
    name TEXT,
    professor TEXT,
    color TEXT,
    notes TEXT,
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
    class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
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
    class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    text TEXT,
    linked_item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS syllabus_topics (
    id TEXT PRIMARY KEY,
    class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    title TEXT,
    done INTEGER DEFAULT 0,
    sort_order INTEGER
);

CREATE TABLE IF NOT EXISTS term_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT,
    start_date TEXT,
    end_date TEXT
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO term_settings (id, name, start_date, end_date) VALUES (1, '', '', '')"
    )
    conn.commit()
    conn.close()

#!/usr/bin/env python3
"""Move the local SQLite data into the deployed Postgres, owned by one account.

Emits SQL rather than connecting, so nothing happens until you look at it and run it.
Ordering matters: rows are written parents-first, using the same dependency walk the
schema generator uses, because Postgres enforces foreign keys that SQLite was letting
slide.

    # 1. sign in to the deployed app once, so the account exists
    # 2. then, pointing at the same database Railway gave you:
    python3 cloud/migrate/export_to_pg.py --email you@example.com > /tmp/import.sql
    psql "$DATABASE_URL" -f /tmp/import.sql

Uploaded files are not SQL and are not included. `materials.stored_name` points at a
file under DATA_DIR/uploads, so those bytes have to reach the Railway volume separately:

    tar -czf uploads.tgz -C data uploads
    # then copy it onto the volume, e.g. with `railway ssh` or a one-off shell

Without that the rows are all present and the app works, but a download 404s.
"""
import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "..", "..", "data", "vesta.db")

# Written by init_db on a fresh install, so it already exists for the account and
# inserting it again would collide on the per-user primary key.
SKIP_TABLES = {"term_settings"}


def lit(v):
    """A SQL literal. None becomes NULL; everything else is quoted text, because every
    column in this schema is text, integer or double and Postgres will cast."""
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        return "decode('" + v.hex() + "', 'hex')"
    return "'" + str(v).replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True, help="the account that will own this data")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    path = os.path.abspath(args.db)
    if not os.path.exists(path):
        sys.exit(f"No database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    tables = [t for t in tables if t not in SKIP_TABLES]

    # A materials row whose bytes are gone would import happily and then 404 on every
    # download, which is worse than not being there: it looks like a working file until
    # someone needs it. Vesta's syllabus import points the materials row at the *same*
    # stored file as the syllabus_imports row rather than copying it, so either side
    # removing the file breaks the other, and that is how these came to be orphaned.
    uploads = os.path.join(os.path.dirname(path), "uploads")
    orphaned = set()
    for r in conn.execute("SELECT id, title, stored_name FROM materials"):
        name = r["stored_name"] or ""
        if name and not os.path.exists(os.path.join(uploads, name)):
            orphaned.add(r["id"])
            print(f"-- skipping material {r['id']}: its file is missing "
                  f"({(r['title'] or 'untitled')})", file=sys.stderr)
    if orphaned:
        print(f"-- {len(orphaned)} material row(s) skipped; re-upload those files in the "
              f"app once you are signed in.", file=sys.stderr)

    # parents before children, same walk as the schema generator
    deps = {t: {f["table"] for f in conn.execute(f"PRAGMA foreign_key_list({t})")
                if f["table"] != t and f["table"] in tables} for t in tables}
    ordered, placed = [], set()
    while len(ordered) < len(tables):
        ready = sorted(t for t in tables if t not in placed and deps[t] <= placed)
        if not ready:
            sys.exit("Circular foreign keys; cannot order the inserts.")
        ordered.extend(ready)
        placed.update(ready)

    w = print
    w("-- Vesta: local data, imported into one account.")
    w(f"-- Source: {path}")
    w(f"-- Owner:  {args.email}")
    w("--")
    w("-- Runs as one transaction: either all of it lands or none of it does.")
    w("begin;")
    w("")
    w("do $$")
    w("declare owner_id uuid;")
    w("begin")
    w(f"  select id into owner_id from auth.users where lower(email) = lower({lit(args.email)});")
    w("  if owner_id is null then")
    w(f"    raise exception 'No Vesta account for {args.email}. Sign in once, then re-run this.';")
    w("  end if;")
    w("  perform set_config('vesta.owner', owner_id::text, true);")
    w("end $$;")
    w("")

    OWNER = "current_setting('vesta.owner')::uuid"
    total = 0
    for t in ordered:
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        if not rows:
            continue
        cols = [c for c in rows[0].keys()]
        quoted = ", ".join(f'"{c}"' for c in cols) + ", user_id"
        # Anything pointing at a skipped material goes too, or the insert fails on a
        # foreign key. Ids are uuids, so matching any column against the set is safe.
        keep = [r for r in rows
                if not (orphaned and any(r[c] in orphaned for c in cols))]
        dropped = len(rows) - len(keep)
        if not keep:
            continue
        w(f"-- {t}: {len(keep)} row(s)" + (f"  ({dropped} skipped: missing file)" if dropped else ""))
        for r in keep:
            values = ", ".join(lit(r[c]) for c in cols) + f", {OWNER}"
            w(f'insert into {t} ({quoted}) values ({values});')
        w("")
        total += len(keep)

    # term_settings exists already, so carry the values across instead of inserting
    term = conn.execute("SELECT * FROM term_settings WHERE id=1").fetchone()
    if term:
        w("-- term settings already exist for the account, so update rather than insert")
        w(f"update term_settings set name={lit(term['name'])}, "
          f"start_date={lit(term['start_date'])}, end_date={lit(term['end_date'])} "
          f"where id = 1 and user_id = {OWNER};")
        w("")

    w("commit;")
    w("")
    w(f"-- {total} rows across {len([t for t in ordered if conn.execute(f'SELECT 1 FROM {t} LIMIT 1').fetchone()])} tables.")
    w("-- Uploaded file bytes are NOT included; see the note at the top of this script.")
    conn.close()


if __name__ == "__main__":
    main()

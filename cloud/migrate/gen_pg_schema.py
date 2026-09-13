#!/usr/bin/env python3
"""Generate the Postgres schema from the local SQLite database.

Hand-writing the DDL for 28 tables would go stale the first time a column was added
locally, and the app's own database is the only complete, migrated record of what the
schema actually is: `db.py` applies ALTER statements at startup, so the file and the
reality differ. So this reads the live SQLite schema and emits Postgres.

Two things are added to every table on the way through:

* a `user_id` column defaulting to `auth.uid()`, which is what makes an INSERT pick up
  its owner without any query mentioning the user;
* a call to `apply_owner_rls`, the helper already used and tested by the existing cloud
  schema, which turns on forced Row Level Security with owner-only policies.

Together those mean the Flask app's 262 existing queries need no user filtering at all:
the database refuses to show one student another's rows.

    python3 cloud/migrate/gen_pg_schema.py > cloud/migrate/pg_schema.sql
"""
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "..", "..", "data", "vesta.db")

# Tables that exist only to serve the local single-user app, or that Supabase owns.
SKIP = {
    "sqlite_sequence",
    "app_settings",        # per-install settings; becomes per-user, handled below
}

# Tables whose rows belong to the installation rather than a student. These keep a
# user_id too, so that "my daily AI limit" is mine and not everyone's.
PER_USER_SINGLETON = {"term_settings", "app_settings"}

TYPES = {
    "TEXT": "text",
    "INTEGER": "integer",
    "REAL": "double precision",
    "BLOB": "bytea",
    "": "text",
}

# Columns the app treats as booleans but SQLite stores as 0/1. Postgres is stricter, and
# the app compares them with bool(), so integer is kept deliberately rather than
# converted: changing the type here would mean changing every comparison in the app.
KEEP_INTEGER = True


# Everything the schema needs in order to stand on its own against a plain Postgres,
# rather than depending on Supabase providing auth.uid() and on 002_rls.sql providing
# the policy helper. Vesta now runs its database on Railway and uses Supabase only to
# issue tokens, so the database has to define these itself.
PRELUDE = '''
-- ---------------------------------------------------------------------------
-- Identity. Supabase issues the tokens; this database only needs to know who a
-- request claims to be, and to have somewhere for rows to hang off so that
-- deleting an account takes its data with it.
-- ---------------------------------------------------------------------------
create schema if not exists auth;

create table if not exists auth.users (
  id         uuid primary key,
  email      text,
  created_at timestamptz not null default now()
);

-- Reads the caller's id out of the request-scoped claim the app sets on the
-- connection. It cannot be spoofed from SQL, because only the app sets it, and the
-- app sets it from a token whose signature it verified.
create or replace function auth.uid() returns uuid
language sql stable as $$
  select nullif(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid
$$;

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated')
    then create role authenticated; end if;
  if not exists (select 1 from pg_roles where rolname = 'anon')
    then create role anon; end if;
end $$;

-- The app connects as the database owner and drops to `authenticated` for the duration
-- of each request. `set role` only works if the connecting role is a member of the
-- target one (or is a superuser), so grant it explicitly rather than relying on the
-- host handing out superuser. Without this every request fails at `set role`.
do $$ begin
  execute format('grant authenticated to %I', current_user);
exception when others then
  null;   -- already a member, or a host that does not allow it; RLS still applies
end $$;

-- ---------------------------------------------------------------------------
-- The policy helper. Owner-only, forced, with WITH CHECK on updates so a row can
-- never be handed to another account.
-- ---------------------------------------------------------------------------
create or replace function apply_owner_rls(tbl text)
returns void language plpgsql as $$
begin
  execute format('alter table %I enable row level security', tbl);
  execute format('alter table %I force row level security', tbl);
  execute format('drop policy if exists %I on %I', tbl || '_select_own', tbl);
  execute format('drop policy if exists %I on %I', tbl || '_insert_own', tbl);
  execute format('drop policy if exists %I on %I', tbl || '_update_own', tbl);
  execute format('drop policy if exists %I on %I', tbl || '_delete_own', tbl);
  execute format(
    'create policy %I on %I for select to authenticated using (user_id = auth.uid())',
    tbl || '_select_own', tbl);
  execute format(
    'create policy %I on %I for insert to authenticated with check (user_id = auth.uid())',
    tbl || '_insert_own', tbl);
  execute format(
    'create policy %I on %I for update to authenticated
       using (user_id = auth.uid()) with check (user_id = auth.uid())',
    tbl || '_update_own', tbl);
  execute format(
    'create policy %I on %I for delete to authenticated using (user_id = auth.uid())',
    tbl || '_delete_own', tbl);
end;
$$;
'''


def q_cols(names):
    """Quote a list of column names. `end` is reserved in Postgres, and quoting is
    valid in SQLite too, so the same spelling works against either database."""
    return ", ".join(f'"{n}"' for n in names)


def pg_type(sqlite_type):
    t = (sqlite_type or "").upper().split("(")[0].strip()
    return TYPES.get(t, "text")


def quote_default(value, pg):
    if value is None:
        return None
    v = str(value).strip()
    if v.upper() in ("NULL",):
        return None
    # SQLite writes CURRENT_TIMESTAMP and string literals the same way Postgres accepts
    if v.upper() == "CURRENT_TIMESTAMP":
        return "now()"
    return v


def columns(conn, table):
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def foreign_keys(conn, table):
    return conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()


def indexes(conn, table):
    out = []
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if idx["origin"] != "c":          # only indexes we created, not PK/unique
            continue
        cols = [r["name"] for r in conn.execute(f"PRAGMA index_info({idx['name']})")]
        if cols:
            out.append((idx["name"], cols, bool(idx["unique"])))
    return out


def unique_sets(conn, table):
    out = []
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if idx["origin"] == "u" and idx["unique"]:
            cols = [r["name"] for r in conn.execute(f"PRAGMA index_info({idx['name']})")]
            if cols:
                out.append(cols)
    return out


def main():
    path = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
    if not os.path.exists(path):
        sys.exit(f"No database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    tables = [t for t in tables if t not in SKIP or t in PER_USER_SINGLETON]

    # Emit in dependency order. SQLite accepts a foreign key pointing at a table that
    # does not exist yet; Postgres does not, and alphabetical order puts
    # calendar_imports (which references classes) before classes. A self-reference,
    # like note_folders.parent_id, resolves inside its own CREATE and is ignored here.
    deps = {t: {f["table"] for f in foreign_keys(conn, t)
                if f["table"] != t and f["table"] in tables} for t in tables}
    ordered, placed = [], set()
    while len(ordered) < len(tables):
        ready = sorted(t for t in tables if t not in placed and deps[t] <= placed)
        if not ready:
            stuck = ", ".join(sorted(t for t in tables if t not in placed))
            sys.exit(f"Circular foreign keys, cannot order: {stuck}. "
                     "Emit these as ALTER TABLE ADD CONSTRAINT instead.")
        ordered.extend(ready)
        placed.update(ready)
    tables = ordered

    w = print
    w("-- Vesta: Postgres schema, generated from the local SQLite database.")
    w(f"-- Source: {path}")
    w("-- Generated by cloud/migrate/gen_pg_schema.py. Do not edit by hand.")
    w("--")
    w("-- Every table carries user_id default auth.uid() and forced owner-only RLS, so")
    w("-- the Flask app's existing queries need no user filtering: the database refuses")
    w("-- to return another student's rows even if a query forgets to ask.")
    w("")
    w("begin;")
    w("")
    w(PRELUDE)

    for t in tables:
        cols = columns(conn, t)
        fks = {f["from"]: (f["table"], f["to"], f["on_delete"]) for f in foreign_keys(conn, t)}
        pk = [c["name"] for c in cols if c["pk"]]

        w(f"-- {'-' * 70}")
        w(f"create table if not exists {t} (")
        lines = []
        for c in cols:
            name, typ = c["name"], pg_type(c["type"])
            # Every identifier is quoted. `events` and `schedule_entries` both have a
            # column called `end`, which SQLite accepts bare and Postgres rejects as a
            # reserved word. Double quotes are valid in both, so the app's own queries
            # can be quoted the same way and stay dual-compatible.
            bits = [f'  "{name}" {typ}']
            if c["notnull"] and name not in pk:
                bits.append("not null")
            d = quote_default(c["dflt_value"], typ)
            if d is not None:
                bits.append(f"default {d}")
            if name in fks:
                ref_table, ref_col, on_del = fks[name]
                action = f" on delete {on_del.lower()}" if on_del and on_del != "NO ACTION" else ""
                bits.append(f'references {ref_table}("{ref_col}"){action}')
            lines.append(" ".join(bits))

        # the column that makes multi-tenancy work
        lines.append("  user_id uuid not null default auth.uid() "
                     "references auth.users(id) on delete cascade")

        if pk:
            if t in PER_USER_SINGLETON:
                # These hold one row per install locally: app_settings keys on 'ai', and
                # term_settings is pinned to id = 1. Multi-user turns that into everyone
                # fighting over the same row, so the owner joins the key. SQLite's
                # CHECK (id = 1) is not reported by table_info and so is dropped here,
                # which is what lets each student have their own id = 1.
                lines.append(f"  primary key (user_id, {q_cols(pk)})")
            else:
                lines.append(f"  primary key ({q_cols(pk)})")
        for u in unique_sets(conn, t):
            lines.append(f"  unique ({q_cols(u)})")
        w(",\n".join(lines))
        w(");")

        w(f"create index if not exists {t}_user_idx on {t}(user_id);")
        for name, cols_, uniq in indexes(conn, t):
            kind = "unique index" if uniq else "index"
            w(f"create {kind} if not exists {name} on {t}({q_cols(cols_)});")
        w("")

    w("-- " + "-" * 70)
    w("-- Owner-only Row Level Security on every table, via the helper the existing")
    w("-- cloud schema already uses (002_rls.sql). Forced, with WITH CHECK on updates so")
    w("-- a row cannot be handed to someone else.")
    w("do $$")
    w("declare t text;")
    w("begin")
    w("  foreach t in array array[")
    w(",\n".join(f"    '{t}'" for t in tables))
    w("  ] loop")
    w("    perform apply_owner_rls(t);")
    w("  end loop;")
    w("end $$;")
    w("")
    # No service_role: that is a Supabase concept, and the database now lives on
    # Railway. The app connects as the database owner and drops to `authenticated` for
    # the duration of each request, which is what puts it under the policies.
    w("grant usage on schema public to anon, authenticated;")
    w("grant select, insert, update, delete on all tables in schema public to authenticated;")
    w("grant usage, select on all sequences in schema public to authenticated;")
    w("revoke all on all tables in schema public from anon;")
    w("")
    w("commit;")
    w("")
    w("-- Every row below must show rls_enabled = true.")
    w("select c.relname as table_name, c.relrowsecurity as rls_enabled")
    w("from pg_class c join pg_namespace n on n.oid = c.relnamespace")
    w("where n.nspname = 'public' and c.relkind = 'r' order by c.relname;")
    conn.close()


if __name__ == "__main__":
    main()

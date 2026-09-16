"""A real Postgres, configured the way the deployment is.

The critical detail is the role. `pgserver` hands you a superuser, and a superuser
bypasses row level security outright, FORCE included, so a suite run as one would
report perfect isolation no matter how broken the policies were. Everything here runs
as `vesta_app`: LOGIN, NOSUPERUSER, no BYPASSRLS, owning its own database. That is the
shape `pg_schema.sql` is written for, and the only shape in which these tests mean
anything.

Postgres has to be up and DATABASE_URL set before `db` is imported, because `db.py`
chooses between SQLite and Postgres at import time. Hence module level, not a fixture.

Run this directory in its own pytest process: the sibling suite deliberately runs
against SQLite, and the two cannot share one interpreter.
"""
import os
import pathlib
import sys
import tempfile

import pgserver

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

_PGDATA = pathlib.Path(tempfile.gettempdir()) / "vesta-test-pgdata"
server = pgserver.get_server(_PGDATA)

server.psql("drop database if exists vesta_test")
server.psql("drop role if exists vesta_app")
# `authenticated` and `anon` are cluster-wide and outlive the database, so a second run
# finds them already there, created by a role that has since been dropped. The schema's
# `grant authenticated to current_user` then fails, its DO block swallows the failure,
# and every connection dies later at `set role authenticated`. Clearing them keeps each
# run honest -- and that swallowed grant is worth knowing about on a real host too.
server.psql("drop role if exists authenticated")
server.psql("drop role if exists anon")
# createrole, because pg_schema.sql creates the `authenticated` and `anon` roles and
# grants `authenticated` to whoever is connecting.
server.psql("create role vesta_app login nosuperuser createrole")
server.psql("create database vesta_test owner vesta_app")

DATABASE_URL = server.get_uri(database="vesta_test").replace("postgres:@", "vesta_app@")
os.environ["DATABASE_URL"] = DATABASE_URL
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vesta-pg-tests-"))
os.environ.setdefault("SUPABASE_URL", "https://stub.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "stub-anon-key")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("HTTPS_ONLY", "0")

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"

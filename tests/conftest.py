"""Test environment.

Everything here has to happen before `app` is imported, because `auth.py` reads its
Supabase configuration into module-level constants at import time and `db.py` decides
between SQLite and Postgres the same way.
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A throwaway SQLite database per run.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vesta-tests-"))
os.environ.pop("DATABASE_URL", None)

# Accounts on, so the gate and every /api/auth route is live. No real Supabase is
# ever reached: tests stub httpx.
os.environ.setdefault("SUPABASE_URL", "https://stub.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "stub-anon-key")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
# The test client speaks http, and a Secure-only cookie would never come back.
os.environ.setdefault("HTTPS_ONLY", "0")
# No daily Canvas check thread. It would wait ten minutes before doing anything, but a
# test run has no business starting it at all.
os.environ.setdefault("VESTA_NO_BACKGROUND", "1")

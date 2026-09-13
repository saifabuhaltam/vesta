# Vesta: what's left

A running list so nothing gets lost between sessions. Anything marked **needs Saif**
cannot be done from a terminal: it needs a dashboard login, an email click, or a
decision only he can make.

_Last updated: 2026-09-13_

---

## Deploying (in progress)

- [ ] **needs Saif** — Set the Railway variables (see "Setting the variables" below).
- [ ] Apply `cloud/migrate/pg_schema.sql` to the Railway Postgres. 570 lines, 29 tables,
      forced row level security on every one.
- [ ] Import the existing data: `cloud/migrate/export_to_pg.py --email <you>` produces
      the SQL. Requires signing in to the deployed app once first, so the account exists.
      Verified locally: 105 inserts, every row count matching.
- [ ] Copy `data/uploads/` onto the Railway volume. Not covered by the SQL import, so
      until it is done the rows exist but a download 404s.
- [ ] Smoke-test the deployment: sign in, check the 4 classes and 44 items are there,
      upload a file, run a Headstart.

## Blocked on Saif

- [ ] **Google Calendar credentials.** `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
      from a Google Cloud project, consent screen published to **Production** (in
      Testing, refresh tokens expire every 7 days). Register the callback URL for
      whatever host the app ends up on. Placeholders are already in `.env`.
      The Calendar API itself is free.
- [ ] **Prove two accounts are isolated on the live deployment.** The same checks pass
      20/20 against a local Postgres, but confirming the deployed database matches needs
      the dashboard and clicking confirmation emails. Do this before any friend gets a
      login.
- [ ] **Invite friends** once the above is done: add their email to the Supabase
      allowlist (one line of SQL in the Supabase SQL editor).

## Decisions waiting on Saif

- [ ] **Delete `cloud/`?** The Worker, R2 integration and four JavaScript shims are
      unused by the Railway architecture. Committed in `fd2db5a`, so deleting is safe
      and reversible. The row level security work already lives in
      `cloud/migrate/gen_pg_schema.py`.
- [ ] **What to do about the Google Calendar work** once deployed. It is written and
      tested but its transport has never executed against Google.

## Documentation (deferred by Saif, 2026-09-13)

- [ ] `README.md` still documents SQLite-only and single-user. Needs the Postgres
      backend, accounts, and the real deployment steps.
- [ ] `DESIGN.md` still describes the Supabase-plus-Cloudflare architecture. Needs
      rewriting for Railway plus Supabase-for-auth-only.
- [ ] A deployment checklist matching what was actually built.

## Known gaps and rough edges

- [ ] **Uploads on the volume.** `DATA_DIR=/data` covers it, but nothing yet verifies
      the volume is actually mounted. Without it every deploy wipes uploads silently.
- [ ] **The SFU exam fallback in `syllabus.py` has never fired.** Across 16 real
      sections, finals always lived in `examSchedule`. Kept as a documented guard, not
      a fix.
- [ ] **Multi-section SFU courses** need the section picker; single-lecture courses skip
      it. Working, but only exercised against four real courses.
- [ ] **`.docx` syllabus import** depends on `python-docx` and so on `lxml`. Fine on
      Railway. Worth remembering if the runtime ever changes.
- [ ] **The sync indicator says "Connecting…"** on the sign-in screen, which is noise
      before anyone has signed in.
- [ ] **`gunicorn` worker count.** Postgres is fine with several; if the SQLite fallback
      is ever used in production, more than one worker risks write locking.

## Done, for reference

- Postgres schema generator, row level security proven with two accounts (17/17)
- `pgshim`: `?` placeholders, dict rows, real `PRAGMA table_info`, per-user provisioning
- Accounts: Supabase-issued, verified server side, session cookie, gate on every `/api`
- Sign-in screen, styled and theme-correct, browser-tested end to end (13/13)
- Data migration verified: every table's row count matches
- Three real bugs fixed in the calendar export: floating times drifting across DST,
  zero-length all-day events, unpadded times breaking matching
- Everything committed and pushed: `fd2db5a`, `c6d64f2`

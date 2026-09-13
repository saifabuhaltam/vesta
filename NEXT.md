# Vesta: what's left

A running list so nothing is lost between sessions. **needs Saif** means it cannot be
done from a terminal: a dashboard login, an email click, or a decision that is his.

_Last updated: 2026-09-13, after the Railway deployment came up._

---

## Deploying

Live at `vesta-production-8a53.up.railway.app`. `/health` reports the truth about any
instance: which database, whether accounts are on, the table count, and whether Railway
actually mounted the volume.

Confirmed working: `database: postgres`, `accounts: true`, `tables: 28`,
`volumeMountedAt: /data`.

- [ ] **needs Saif** — Sign in once, so the account row exists for the import to hang
      off. `saifabuhaltam@gmail.com`, already on the Supabase invite list.
- [ ] Run `cloud/migrate/import.sql` against the Railway Postgres. 103 inserts:
      4 classes, 44 items, 6 notes, 27 syllabus topics, 3 grade categories, 4 meeting
      times. Verified end to end against a local Postgres.
- [ ] Get four files into the app. Two screenshots live only on this Mac; the two
      syllabus PDFs are in `test-syllabi/`. Either re-upload all four through the UI
      (two minutes, no tooling) or install the Railway CLI and copy them onto the
      volume.
- [ ] Smoke-test the live site: sign in, confirm the classes and assignments, upload a
      file, run a Headstart, export the calendar.

## Bugs found and not yet fixed

- [ ] **Syllabus import shares one file between two tables.** `do_import` points the
      new `materials` row at the *same* stored file as the `syllabus_imports` row
      rather than copying it. One file, two owners, so whichever side is deleted first
      silently breaks the other. This is what orphaned the PHIL 110 and REM 388
      syllabus rows, whose `syllabus_imports` status still reads `imported` while the
      bytes are gone. **It will happen again** to the next syllabus imported and later
      tidied up. Fix: copy the file at import time, or reference-count it.
- [ ] **`extracted_text` is empty for every material.** All four rows have length 0, so
      uploaded files are not searchable and Headstart cannot read them. The extraction
      runs on upload and there is a startup backfill, so something is not firing. Worth
      investigating before relying on file search.
- [ ] **Commit `7b5d88f` has a Python script as its commit message.** My heredoc
      nesting error: the message and the script were swapped. The code in it is correct.
      Fixing means `git commit --amend` plus a force-push, which rewrites pushed
      history, so **needs Saif** to say go.
- [ ] **The sync indicator reads "Connecting…" on the sign-in screen**, before anyone
      has signed in. Cosmetic, but it is the first thing a new user sees.

## Blocked on Saif

- [ ] **Rotate the Google OAuth client secret.** One was pasted into a chat transcript
      on 2026-09-13 and must be considered compromised. Google Cloud Console →
      Credentials → that OAuth client → add a new secret and delete the exposed one, or
      delete the client and make a fresh one. Nothing had been wired to it yet, so
      there is nothing to redo. Neither the client ID nor the secret ever needs to pass
      through a conversation: both go from the Google console straight into the
      Supabase provider page and Railway's variables.
- [ ] **Google Calendar credentials.** `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
      from a Google Cloud project with the consent screen published to **Production**
      (in Testing, refresh tokens expire every 7 days). The redirect URI must now be
      the Railway domain, not localhost. Placeholders are in `.env`. The API is free.
- [ ] **Prove two accounts are isolated on the live deployment.** The same checks pass
      against a local Postgres, but confirming the deployed database matches needs the
      dashboard and clicking confirmation emails. **Do this before any friend gets a
      login.**
- [ ] **Invite friends**: add each email to the Supabase allowlist, one line of SQL.

## Decisions waiting on Saif

- [ ] **Delete `cloud/`?** The Worker, R2 integration and four JavaScript shims are
      unused by the Railway architecture. Committed in `fd2db5a`, so deleting is safe
      and reversible. The row level security work already lives in
      `cloud/migrate/gen_pg_schema.py`.
- [ ] **Google Calendar sync**: written and tested, but its transport has never
      executed against Google. Decide whether to finish it or park it.
- [ ] **Amend the bad commit message** on `7b5d88f`, which needs a force-push.

## Documentation (deferred by Saif, 2026-09-13)

- [ ] `README.md` still documents SQLite-only, single-user, and `localhost:5000`.
      Needs Postgres, accounts, Railway, and the real deployment steps.
- [ ] `DESIGN.md` still describes Supabase-plus-Cloudflare. Needs rewriting for
      Railway, with Supabase used only to issue tokens.
- [ ] A deployment checklist matching what was actually built.

## Known gaps and rough edges

- [ ] **The SFU exam fallback in `syllabus.py` has never fired.** Across 16 real
      sections, finals always lived in `examSchedule`. Kept as a documented guard, not
      a fix for an observed bug.
- [ ] **Multi-section SFU courses** were only exercised against four real courses.
- [ ] **`.docx` syllabus import** needs `python-docx` and therefore `lxml`. Fine on
      Railway; worth remembering if the runtime ever changes.
- [ ] **`gunicorn` worker count.** Postgres handles several fine. If the SQLite
      fallback is ever used in production, more than one worker risks write locking.
- [ ] **Session length is 30 days** (`SESSION_SECONDS` in `auth.py`). A removed Supabase
      account keeps working locally until its cookie expires.

## Done

- Postgres schema that builds itself on first boot, 28 tables, forced row level
  security on every one, dependency-ordered
- `pgshim`: `?` placeholders, dict rows, a real `PRAGMA table_info`, per-user
  provisioning, so none of the app's 262 queries needed a user filter
- Two-student isolation proven with the app's own SQL (17/17)
- Accounts: Supabase issues the token, Flask verifies and holds the session, every
  `/api` route gated, local single-user mode untouched
- Sign-in screen, theme-correct, browser-tested end to end (13/13)
- `/health`, so "is it really on Postgres" is a fact rather than a hope
- Three real bugs fixed in the calendar export: floating times drifting an hour across
  DST, zero-length all-day events, unpadded times breaking matching
- `psycopg` and `httpx` added to requirements; without them the deploy built fine and
  then crashed on first use
- Commits: `fd2db5a`, `c6d64f2`, `dcdc8ba`, `ccdb050`, `7b5d88f`

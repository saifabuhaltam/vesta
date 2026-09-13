# Vesta in the cloud

Multi-user Vesta on Supabase, Cloudflare R2 and Cloudflare Workers. The local Flask
app in the parent directory keeps working untouched until this is finished.

---

## What I changed about your plan, and why

Your stack choice is right for the budget. Three adjustments are worth knowing about
before you start clicking around in dashboards.

**The browser talks to Postgres directly, not through a Worker.** You described
Workers as the backend for everything. Routing normal reads and writes through a
Worker would mean the Worker holds a database connection, and Workers cannot open a
plain Postgres TCP connection without Hyperdrive, which is a paid add-on. It would
also double your latency for no gain. Supabase publishes a REST layer over your
database, and Row Level Security decides what each request may touch. So the browser
queries Postgres directly using the public anon key, and the policies in
`supabase/002_rls.sql` are what keep users apart.

The Worker stays, but only for the two jobs that genuinely need a secret the browser
must never see: talking to R2, and calling the Anthropic API for Headstart.

**Signup is gated by an email allowlist, not an invite code.** You said you and a few
friends. A redeemable code has to survive the signup request, and the Google sign-in
round trip gives us nowhere reliable to carry one. An email is present on every new
account no matter which provider it came from, so one check covers both. You add
people with one line of SQL.

**Notes do not need real collaborative editing.** You asked for Google Docs behaviour.
The part of Google Docs that is genuinely hard is two people typing in one paragraph
at the same time, which needs conflict-free replicated data types and is weeks of
work. Vesta accounts are single-person, so nobody is ever racing you. What you
actually want is instant local updates, debounced autosave, a Saving/Saved indicator,
offline recovery and version history. All of that is straightforward, and the schema
already supports it. If you ever want shared notes between accounts, that is the
point to revisit this.

---

## What it costs

Everything below sits inside free tiers at your scale.

| Service | Free allowance | What Vesta uses it for |
|---|---|---|
| Supabase | 500 MB database, 50,000 monthly users | Auth, all structured data, realtime |
| Cloudflare R2 | 10 GB stored, no egress charge ever | Uploaded PDFs, slides, images |
| Cloudflare Workers | 100,000 requests/day | R2 access, Anthropic calls |
| Cloudflare Pages | Unlimited requests | Hosting the app itself |

R2 charging nothing for egress is the reason it beats Supabase Storage here. Every
time you open a lecture PDF you are downloading it again, and on most providers that
is the line item that grows.

Two things to watch:

- **A free Supabase project pauses after 7 days with no activity.** During term you
  will be in it daily so it will not trigger. Over a long break it can, and you
  restore it with one click. Supabase Pro at $25/month removes the pause and adds
  daily backups. You said you have $25 available. My advice is to stay free until
  Vesta is genuinely your daily driver and losing a week of data would hurt, then
  upgrade for the backups rather than the pause.
- **The Anthropic API is not free and is not part of any of this.** Headstart calls
  cost real money per generation. That is already true of your local app. Set a
  spend limit in the Anthropic console.

---

## Setup

### 1. Supabase

Create a project at supabase.com. Free plan. Pick a region near you.

Open the SQL editor and run, in order:

1. `supabase/001_schema.sql` — tables, indexes, constraints
2. `supabase/002_rls.sql` — Row Level Security and grants
3. `supabase/003_functions.sql` — signup gate, note history, retention
4. `supabase/004_study.sql` — grade categories, flashcards, quizzes

Or paste `setup/all_in_one.sql`, which is all four concatenated. Regenerate it
with `python3 cloud/setup/build_sql.py` after adding a migration.

If `003` reports that pg_cron could not be scheduled, enable **pg_cron** under
Database → Extensions and run that file again. Without it the 30-day Trash never
empties, which is not urgent but does mean deleted things accumulate.

Add yourself to the allowlist. Nobody can create an account without this:

```sql
insert into allowed_emails (email, note) values
  ('your.real@email.com', 'me');
```

### 2. Google sign-in

Free. In Google Cloud Console create a project, then an OAuth 2.0 Client ID of type
Web application. Set the authorised redirect URI to the value Supabase shows you
under Authentication → Providers → Google. Paste the client ID and secret back into
Supabase and enable the provider.

Email and password is enabled by default as a fallback, and works for anyone on the
allowlist without any of the above.

### 3. Bring your existing data over

Sign in to the deployed app once first, so your account exists. Then:

```bash
python3 cloud/migrate/export_sqlite.py --email your.real@email.com > cloud/migrate/import.sql
```

It reads `data/vesta.db` read-only and never writes to it. Paste the result into the
Supabase SQL editor and run it. Every id is preserved, so nothing that referenced
anything is broken by the move.

Uploaded files are the exception. Their bytes are on your laptop, so their rows
import marked `pending` and the script writes `upload_manifest.json` listing what
still needs pushing to R2. They will show in the app as uploads that never finished
until then, which is deliberate: a file row is only ever marked complete once the
bytes are confirmed to exist.

### 4. Cloudflare

Create an R2 bucket named `vesta-files`. No CORS policy is needed: the browser
never talks to the bucket, only to the Worker.

```bash
cd cloud/worker
npx wrangler secret put SUPABASE_URL
npx wrangler secret put SUPABASE_ANON_KEY
npx wrangler secret put SUPABASE_SERVICE_KEY
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put UPLOAD_TICKET_SECRET   # openssl rand -hex 32
npx wrangler deploy
```

Then put the Worker's URL and your Supabase project URL and anon key into the
frontend config. Edit `ALLOWED_ORIGIN` in `wrangler.toml` to match wherever the app
is actually served from.

---

## Security model, in one paragraph

The anon key ships in the page and is meant to be public. It identifies the project,
it does not grant access. What grants access is a signed JWT from Supabase Auth, and
what limits it is Row Level Security: every table has RLS forced on, and every policy
reduces to `user_id = auth.uid()`, which reads the caller's token and cannot be
forged from the browser. Tables that no user should ever touch, the invite allowlist
and the R2 deletion queue, have RLS on and no policies at all, which denies everyone.
The service role key bypasses all of it and therefore lives only in Cloudflare
Worker secrets, never in the frontend, never in git.

This is verified rather than assumed. The test suite creates two accounts and then
actively tries to break the boundary: reading another user's rows, updating them,
deleting them, inserting a row owned by someone else, and handing your own row to
another user. All are refused.

---

## Status

| Piece | State |
|---|---|
| Postgres schema, all entities | Done, tested |
| Row Level Security and grants | Done, 33 checks passing |
| Signup allowlist and profile creation | Done, tested |
| Note version history with autosave throttling | Done, tested |
| 30-day Trash and retention job | Done, scheduled |
| File dedupe and multi-place linking | Done, tested |
| R2 deletion queue | Done, drained by the Worker's nightly cron |
| Migration from local SQLite | Done, verified against real data |
| Cloudflare Worker: R2 upload, download, AI proxy | Done, 25 checks passing |
| Frontend auth and Supabase data layer | Done, 22 checks passing |
| Notes offline recovery via IndexedDB | Done, wired into the editor |
| Realtime cross-device sync | Done, coalesced so typing is never interrupted |
| Deployment to Cloudflare | Live at vesta.saifabuhaltam.workers.dev |
| Grade categories with weights and drop-lowest | Done, tested |
| Flashcards with spaced repetition scheduling | Schema done, tested, no UI yet |
| Quizzes, questions and attempts | Schema done, tested, no UI yet |
| Password reset by email | Done, tested |
| Persistent sessions across reload and tabs | Done, 16 checks passing |
| Account panel, display name, sign out | Done |
| Two-account isolation, local mirror of the deployed SQL | Done, 20/20 |
| The live script itself, run against a Supabase test double | Done, 14/14 |
| Two-account isolation on the live deployment | Blocked on the owner: see SETUP.md, "Outstanding" |
| Re-running the SQL over an existing database | Done, 11/11, data preserved |

### Running the tests

```bash
.venv/bin/python  scratchpad/test_schema.py    # schema + RLS, 33 checks
.venv/bin/python  scratchpad/test_study.py     # grades, flashcards, quizzes, 27 checks
.venv/bin/python  scratchpad/test_acceptance.py # two accounts vs deployed SQL, 20 checks
.venv/bin/python  scratchpad/test_rerun.py     # re-running SQL is safe, 11 checks
python3 scratchpad/test_session.py             # sign in, stay in, sign out, 16 checks
.venv/bin/python  scratchpad/test_import.py    # migration round trip, 17 checks
osascript -l JavaScript scratchpad/worker_test.js   # Worker security logic, 25 checks
osascript -l JavaScript scratchpad/shape_test.js    # state shape vs Flask, 10 checks
python3 scratchpad/test_wiring.py              # backend switch diverts, 10 checks
python3 scratchpad/test_gate.py                # built bundle + sign-in, 12 checks
```

The shape test is the one that matters most for the migration: it takes the rows a
real Postgres returns after the import, runs them through the adapter, and compares
the result field by field against what the running Flask app serves from
`/api/state`. Every field the UI reads is identical, which is why no view code had
to change.

The Worker tests cover the parts that decide who gets access: upload tickets cannot
be forged, extended, reused for another file, or reused by another user; CORS never
wildcards; a filename cannot escape its folder in R2; and Headstart cannot be turned
into an open-ended prompt endpoint.

Both boot a real PostgreSQL from a Python wheel, so they need no database installed.
Note that macOS has twice quarantined and deleted the bundled `postgres` binary after
a run; if a test suddenly reports that `postgres` is missing, reinstall with
`.venv/bin/python -m pip install --force-reinstall --no-cache-dir pgserver`.


---

## Deploying the app itself

`static/index.html` is never edited for the cloud. The build injects the cloud
scripts ahead of the app's own script, which is what flips the backend switch
inside it. Without those scripts the very same file talks to Flask, which is how
the local app keeps working.

```bash
cp cloud/app/config.example.js cloud/app/config.js   # fill in your URLs and anon key
python3 cloud/build.py
npx wrangler pages deploy cloud/dist --project-name vesta
```

Then add the Pages URL to `ALLOWED_ORIGIN` in `cloud/worker/wrangler.toml` and
redeploy the Worker, or uploads will be refused by CORS.

## Proving isolation on the live deployment

Everything else is tested against a local database. This one talks to the real
project the way a browser does:

```sql
insert into allowed_emails (email, note) values
  ('you+test1@gmail.com','test A'), ('you+test2@gmail.com','test B');
```

```bash
python3 cloud/setup/test_two_accounts.py you+test1@gmail.com pw1 you+test2@gmail.com pw2
```

It creates a class as A and checks B cannot see it, fetch it by id, rename it or
delete it, then cleans up. If signup reports that confirmation is required, turn
off Authentication → Sign In / Providers → Email → Confirm email. The invite list
already decides who may register, so confirmation adds little for an invite-only
app and it blocks automated checks.

## What is left

- Rubric parsing still lives in Flask and has not moved to the Worker. In the cloud
  it reports that clearly rather than failing quietly.
- Text extraction from PDFs and DOCX (which feeds Headstart) happens in Python
  today. It needs a home in the Worker or a queue.
- The Files page does not yet surface Trash, version history or resumable uploads
  in the interface. The backend for all three is built and tested; only the screens
  are missing.

# Vesta: what's left

A running list so nothing is lost between sessions. **needs Saif** means it cannot be
done from a terminal: a dashboard login, an email click, or a decision that is his.

_Last updated: 2026-09-20. Rewritten from scratch: the old file had grown to 1,228
lines, most of it a record of work already finished, and 72 open boxes of which about
ten were stale. What follows is what is actually open, and the history worth keeping._

Live at **https://vesta.study**, auto-deploying from `main`. `/health` reports the
truth about any instance: which database, whether accounts are on, the table count,
whether Railway mounted the volume, which migrations ran, and how many Google push
channels are alive.

---

## Open now

### Before friends lean on it

- [ ] **needs Saif** — Sign in on the live site and use it for an evening. Everything
      below this line has been driven in a browser against a local copy; the live
      instance has never been clicked through signed in. Worth doing in one sitting:
      add a class, import a syllabus, upload a file, run a Headstart, start a chat,
      sit a quiz, run the Humanizer, connect the calendar, export it.
- [ ] **needs Saif** — Prove two accounts are isolated on the deployed database. The
      checks pass against a local Postgres configured the same way, and `/health`
      reports `rlsEnforced: true`, but confirming the deployment itself needs two real
      logins and the confirmation emails that come with them. **Before any friend
      relies on it.**
- [ ] **The account screen and the set-a-new-password screen have never been opened in
      a browser.** The routes behind them are tested and the JavaScript parses; nobody
      has clicked them. They are the two screens a new user meets first if anything
      goes wrong.
- [ ] **Streaming has not been watched on the live site.** Replies stream locally. If
      they arrive all at once at the end on vesta.study, something between Railway and
      the browser is buffering; `X-Accel-Buffering: no` is already set.
- [ ] **The Humanizer has not been run signed in on the live site**, and a 4,000-word
      run has never gone to the real model. Measured runs went to 1,305 words in 63
      seconds; 4,000 in one piece should be about three minutes and roughly 25 cents.
      Above that it is split into sections automatically.
- [ ] **Google push has never spoken to Google.** The channel registration, renewal,
      webhook handling and the traps around them are tested against a stub. Nothing
      registers locally, because Google will not deliver to localhost, so the first
      real test is on vesta.study: connect the calendar, sync once, then check
      `/health` reports `calendarPush.channels`, and change something in Google to see
      whether it lands without pressing Sync.

### Decisions waiting on Saif

- [ ] **Connected devices in Settings.** Saif said the account screen is good as it is,
      and that a list of signed-in devices would be nice later. Not built. It needs a
      sessions table; the session is a signed cookie today and nothing records where it
      was issued.
- [ ] **Amend the bad commit message** on `7b5d88f`, whose message is a Python script
      (a heredoc nesting error; the code in it is correct). Fixing it means a
      force-push, which rewrites pushed history. Saif's call, and leaving it is fine.

### Known rough edges

- [ ] **Only the Classes screen hides its create buttons in an archived term.** The
      dashboard, the assignments page and the class page now hide theirs too, but the
      calendar, notes and files pages still offer actions that the server refuses with
      a 423 and a clear message. Worth tidying, not wrong.
- [ ] **`term_settings` is unused.** Every reader moved to the active semester. The
      table and its rows are left alone rather than dropped, but it should come out in
      a later migration once this has been live a while.
- [ ] **`cloud/migrate/pg_schema.sql` is stale** — no `file_folders`, and none of the
      columns added since. Harmless, because `run_pg_migrations()` runs straight after
      `ensure_pg_schema()` and every migration is `if not exists`, so a brand-new
      database is corrected a second later. Regenerating it means running
      `gen_pg_schema.py` against a current local SQLite database.
- [ ] **Any SQLite ALTER needs a partner block in `pg_migrations.sql` on the same day.**
      Twice now the two paths have diverged and a feature reached production with none
      of its schema. Every migration added since (006 to 009) has its partner. Better
      than a rule would be a test that diffs the two schemas and fails.
- [ ] **Session length is 30 days**, so removing someone from `INVITE_EMAILS` does not
      sign them out until their cookie expires. The list is checked at sign-in, not per
      request.
- [ ] **The schema's grant block hides its own failure.** `grant authenticated to
      current_user` sits in a DO block with `exception when others then null`, and its
      comment claims "RLS still applies" if it fails. That is wrong: when the grant
      fails, every connection dies at `set role authenticated` and nothing works at
      all. Worth failing loudly.
- [ ] **A stopped reply's output tokens are estimated** from the characters that
      arrived, because the final usage never comes back on an aborted stream.
- [ ] **No folder backfill on Postgres.** Classes that existed before file folders
      shipped have no default folders until something is uploaded into them, because
      `folder_id_for_kind` creates them on demand. Files keep their `category` either
      way, so nothing is lost or hidden.
- [ ] **Multi-section SFU courses** were only exercised against four real courses, and
      the SFU exam fallback in `syllabus.py` has never fired across 16 real sections.
      Both are documented guards rather than fixes for observed bugs.

### Documentation (still deferred)

- [ ] `DESIGN.md` still describes Supabase-plus-Cloudflare. It needs rewriting for
      Railway, with Supabase used only to issue tokens.
- [ ] A deployment checklist matching what was actually built, including checking
      `/health` for `migrations` after any deploy that carries a schema change.

### Deliberately parked

- **Separate object storage for uploads (R2).** Decided against on 2026-09-20: uploads
  work on the Railway volume and survive redeploys, and moving them would add a second
  service, new credentials and a migration of existing files to solve a problem that
  has not appeared. The consequence accepted: the volume is a single point of failure,
  backups are ours, and the 25 MB per-file cap in `app.py` stays until it hurts.
- **Friends and sharing.** Accounts work, but the row level security policies are
  strictly owner-only, so sharing a class or a note is not a new join table, it is a
  policy rewrite across every table. Worth designing before it is promised.
- **Account deletion.** Ruled out by Saif on 2026-09-15. Wiping every row is the same
  thing by another name, so the Data section in Settings says so and offers an export
  instead.
- **Two-factor sign-in and a profile photo.** Saif said Settings is good as it is.
- **Categorising assignments added by a later syllabus document.** Saif does not need
  them categorised.

---

## Built on 2026-09-20

The whole of this session, in the order it happened. Each is live unless it says
otherwise.

### Everything got faster

Two things made saving feel slow, and both are fixed.

`/api/state` serialised each class and each assignment one at a time, so a term with
five classes, sixty assignments and forty files cost **277 queries**. Against Postgres
over a network that is seconds, and the page reloaded all of it after every edit.
`state_children()` loads every child table in one query each: **22 queries** for the
same term, byte-identical JSON.

The page waited for the server and then reloaded the whole term before the screen
moved. Assignments, classes, files, notes, folders, syllabus topics and events now
paint the change immediately, send it, and fold the saved row back in when it lands; a
refused write puts the old row back and says so. Measured in a browser with every
response held for 1.2 seconds: a status change paints in **10 ms**, issues one write,
and reloads no state at all.

### The seven reported bugs

Rubrics are reachable from an assignment, and a rubric parsed there links itself to
that assignment, so "Break down the rubric" stops running with no rubric. Practice
tests and decks show on the assignment they belong to (decks gained an `item_id`).
Notes linked through the Links menu appear on the card. A new note from a card opens
the note. An assignment with no class can start one, in the Inbox. An archived term
stops offering work it will refuse. The deck screen is no longer a wall of red.

### Learn and Test

Both were coming-soon buttons; both work now, and neither calls the model, because a
set you typed yourself should not cost anything to study. Learn is a loop: a term is
learned at level 2, recognised from four choices then typed from memory, and a wrong
answer drops it to zero and puts it back in the round. The level lives on the card, so
closing the tab or picking the set up on a phone carries on where you left off. Test is
one scored sitting of mixed question types, marked leniently, with anything it cannot
judge handed back to you.

### A phone layout

Below 820px the sidebar becomes a drawer with a scrim, opened from a fixed top bar.
The page gets the full width, modals become bottom sheets, and inputs are 16px so iOS
stops zooming in. The calendar's month grid scrolls sideways inside its own box rather
than squeezing seven columns into 393px. 18 checks at 393x852.

### One home for generated work

Saved Headstarts are rewritten at boot as chats carrying the tool's name and its
output, and the old row records which chat it became. Nothing is deleted, and the
migration is idempotent and runs once per account. The one-shot tools stream now too,
so nothing in Vesta sits blank for thirty seconds any more.

### Chats, not threads

Renamed everywhere a person reads the word. The table, the routes and the code keep
their names.

### The rest

Move a class between terms, carrying its assignments, files, notes and grades. Search
inside file contents, which also settled a question open since 2026-09-14: extraction
does work on a real publisher PDF (the PHIL 110 syllabus gives 7,545 characters). The
Humanizer takes a whole paper by splitting it at paragraph boundaries, and can read a
file already in Vesta. Quizzes are screens, listed on Study, and every screen has a
URL, so Back works and a chat can be linked to. Nothing offers to spend money with
nothing to read. Google push notifications. The Procfile binds explicitly. The dead
`cloud/` files and their stale signing key are gone.

---

## How this app is built, and what has bitten before

Worth reading before changing anything structural. Each of these cost real time to
learn.

### Schema changes reach a deployed database only through `pg_migrations.sql`

`init_db()` returns early when `DATABASE_URL` is set, so everything below that return
is the SQLite story. A feature can be fully tested locally, pass every check, and
reach production with none of its schema; the failure is not at boot, it is
`relation "x" does not exist` on the first `/api/state`, which is every page load.
`run_pg_migrations` applies each block once, under an advisory lock, recorded in
`schema_migrations`. A failed migration is a failed deploy, because the app raises at
boot rather than serving a half-migrated database: deploy one when there is time to
read the log.

### Anything that runs outside a request must go through `for_each_account`

On Postgres, `db.get_db()` with no user matches zero rows and reports success having
read and written nothing, because forced RLS compares `user_id` against a null
`auth.uid()`. That was the Office preview bug. Worse, on a host that hands out a
superuser — Railway does — a query that never calls `become()` reads **every**
account's rows. It fails open, not closed. Treat any new `get_db()` outside a request
as a security question, not a correctness one.

### Accounts are isolated, and that is measured rather than assumed

Every app table carries `user_id` with forced row level security and owner-only
policies. `tests/pg` runs a real Postgres as a NOSUPERUSER role with no BYPASSRLS,
which is the only configuration in which any of it means anything: as a superuser every
policy is bypassed and the suite would pass while isolating nothing.

### Deploying

Turning on auto-deploy does not deploy what is already pushed; it fires on the next
commit. "Could not load branches" means Railway lost its GitHub access, fixable at
github.com/settings/installations. A service can be linked to a template as well as a
repo, and pressing **Update** on the upstream banner pulls the template's version over
the service's own configuration. Never infer that auto-deploy works because the live
version matches a commit: `/health` reports what is running, never how it got there.
Railway runs Python 3.13 and local development runs 3.9, so nothing here tests against
the runtime that serves it.

### Testing in a browser

`reference/speed/` holds the harnesses: `test_instant.py` (edits paint before the
server answers), `test_bugfixes.py`, `test_learn.py`, `test_phone.py`, and
`stub_stream.py`, which runs Vesta with a model that streams canned text so "does it
appear as it is written" is answerable without spending anything. Any test that clicks
inside a scrolling container has to pick an element already on screen: clicking one
that is scrolled out makes the browser scroll to it first, and the test then moves the
page and blames the app.

### Cost control

`chat_guard` and `prompt_guard` refuse before spending, so a refusal is an ordinary
JSON answer decided before a stream opens. The pinned material goes in `system` with a
cache breakpoint after it, and a test asserts the cached prefix is byte-identical
between turns, which is the invariant the whole "ten turns for $0.10" figure depends
on. `AI_GLOBAL_DAILY_CAP_USD` bounds spend across every account; the per-account cap
lives in `app_settings` and anyone can raise their own, so it is not a spend control.

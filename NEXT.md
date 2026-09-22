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
- [ ] **The last round trip before the calendar appears.** A return visit now draws
      the term the moment the server says who is signed in: one request, about 1.3
      seconds on a connection holding every response for 1.2 seconds. Removing even
      that means trusting the device's copy *before* the answer arrives, which needs a
      readable marker cookie set beside the session cookie so the page can check the
      account itself. It would make the calendar appear instantly on every visit. The
      cost is a window -- an expired session on a shared browser -- where the previous
      account's term is on screen for one round trip before the sign-in screen
      replaces it. Saif's call, because it is his friends' accounts.
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
- [ ] **A redraw held back while writing happens 400 ms after the caret leaves the
      editor**, so if a background sync landed mid-sentence, clicking away can scroll
      the notes page back to the top once. Any click that redraws on its own cancels
      it, so it is only visible when you click something inert.
- [ ] **needs Saif** — The device copy in `localStorage` holds the whole term,
      including the full text of every note. It is only painted for the account it
      was saved under and `forgetState()` clears it on sign-out, but closing the
      browser without signing out leaves it on disk, and it is only overwritten when
      someone else signs in on that browser. On your own machine that is fine. If a
      friend uses Vesta on a shared or library computer it is worth knowing about.
      Dropping note bodies from the cache would cut most of the size and all of the
      sensitive part, at the cost of a blank note for one round trip when a link
      opens straight onto one. Your call, because it is the feature's whole point.
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

## Built on 2026-09-21

### Moving a file reported failure after succeeding

With the 500s gone, the move itself worked and the page still said *"Something went
wrong — (intermediate value).then is not a function"*. The 500 had been hiding it.

`loadState(pre)` takes an optional **prefetch**: a promise for the state payload that
was already in flight. A dozen call sites say `.then(loadState)`, and a `.then` handler
is handed the resolved *value* of the call before it, not a promise. So any route that
answers with a body -- `{id:...}`, `{ok:true}`, a saved row -- arrived as a plain
object, which is truthy, and `pre.then(...)` threw. `loadState` now treats `pre` as a
prefetch only when it is actually thenable, which repairs every one of those call sites
at once rather than one at a time.

`reference/speed/test_file_move.py` drives the real drag in a browser and fails on the
unfixed page with Saif's exact message. A unit test could not have caught this: nothing
is wrong on the server, and the entire symptom is a dialog.

### The file tile's delete button came back out

Added earlier the same day, removed at Saif's request: the `✕` sat directly under the
star on a small tile and was too easy to hit by accident. Deleting a file is still in
the Files page's list layout. If the grid needs one again it should be the `⋯` menu the
folder cards already use, where a second click confirms the intent.

### Uploading and moving files stopped returning 500

Saif: *"why am i getting a request failed (500) everytime i try to upload a file or move
it around in my class"*, then, usefully, the order it happened in: five readings went
into PSYC 300W, the sixth failed, a different file worked, and moving that file into the
Readings folder failed too.

Three separate causes, every one of them Postgres-only and invisible locally. `IS ?` in
`create_file_folder` and `add_material`; `folder_id` assigned twice in one UPDATE when a
drag sends `classId` and `folderId` together; and a NUL byte in the text `pypdf`
extracted from that sixth reading. They are written up under **SQL that runs on SQLite is
not SQL that runs on Postgres** above.

Separately: a file looked undeletable. The `✕` existed only in the Files page's *list*
layout, and the page opens in *grid*, so unless you switched layouts there was no delete
anywhere. The tile now carries one, revealed on hover.


### Notes stopped throwing away what was being typed

Saif: *"notes keep cutting out while typing and resetting the page and resets the last
thing that i was trying to type."* Four separate things were doing it.

Autosave held the **latest patch only**, on a 700 ms timer that restarted on every
keystroke. So typing without a 700 ms pause never saved at all; editing the title and
then the body inside one window threw the title away; and `flushNoteSave()`, called
whenever you left a note, **cancelled the pending write instead of sending it** — the
last thing typed before clicking anywhere was discarded on purpose. Autosave now holds
one merged patch per note, sends it after 700 ms idle *or* 2.5 seconds regardless, and
flushing actually writes. It also flushes on `pagehide` and when the tab is hidden.

And any full redraw landed on top of the editor. `adoptState` called `render()`
unconditionally, so a Google Calendar poll — every three minutes, and on every window
focus — rebuilt the note from the server's copy: scroll to the top, caret gone, the
last few seconds of writing painted over. Now the cached note is patched on every
keystroke, so a redraw paints what is on screen rather than what the server last heard;
and while the caret is inside any rich-text editor the redraw is held back entirely
until writing stops.

### A new assignment stopped vanishing a second after it appeared

Found while reviewing the eager-create work rather than reported. Every eager write
closes over the array it painted into, so it can drop the saved row in or take a
refused one back out. A state load replaced those arrays outright, so a reload landing
between "Save" and the server's answer -- a Google poll, or any other save that
reloads the term -- left the create holding an array nothing draws from. The
assignment appeared, vanished, and only came back on the next reload. It was on the
server the whole time, which is the worst version: the screen said the work was lost
when it was not.

State loads now refill the arrays in place instead of replacing them, which fixes the
same latent problem in every eager write, not just creates. And a create whose
temporary row was swept away by a reload puts the saved row back rather than dropping
it, unless that reload was late enough to carry it already.

Reproduced first, in `reference/speed/test_eager_reload.py`: hold `POST /api/items`
inside the page for three seconds, create a note in the gap to force a reload, and
watch the row disappear from a screen whose server has it. Six checks.

### The device copy is written only when it changed

`rememberState` stringified the whole term and wrote it to `localStorage` on every
state load -- every three-minute poll and every save that reloads -- on the main
thread, when the stored copy was almost always already that exact payload. It now
writes when the signature has actually moved.

### The editor answers the keyboard

`cmd/ctrl + B`, `I`, `U`, `shift+X` for strikethrough, and `shift+7` / `shift+8` for
numbered and bulleted lists. Applied by hand rather than left to the browser, whose own
shortcuts differ between Chrome and Safari and tell the autosave nothing changed.

Markers now fire on **space**, the way Notion does: `- `, `* ` and `+ ` open a bullet,
`1. ` a numbered list, `# ` `## ` `### ` headings, `> ` a quote, `[] ` a checklist,
``` ``` ``` a code block, `--- ` a divider. Tab still finishes a marker too, and still
nests inside a list.

None of this worked on the first line of a note, which is the line people actually
start a list on. The marker was looked for on the *block element* around the caret, and
text typed into a note that has never been formatted has no block element — it sits
directly under the editor. The caret's own text run is read instead, and the line is
given a paragraph when a command needs one (`formatBlock` cannot work without one,
which is why `# ` did nothing while `- ` worked).

Checked in a browser by `reference/speed/test_notes_editor.py` — 12 checks, each named
after the original complaint.

---

## Built on 2026-09-20

The whole of this session, in the order it happened. Each is live unless it says
otherwise.

### The calendar stopped waiting for the server

Everything inside the calendar already redrew in under 10 ms. What was slow was
everything that had to reach the server first, and the calendar was where it showed.

Adding an event cost two round trips with the modal frozen in front of it: the write,
and then a reload of the entire term. Measured against a server holding every response
for 1.2 seconds, that was **2,448 ms** of nothing happening. Events, and assignments
created from the form, are now painted under a temporary id the moment Save is pressed
and the saved row takes their place when it lands: **6-22 ms**, one write, no reload.
Editing and deleting were already painted immediately but kept the modal open until
the round trip finished; the modal closes with the change now. A refused write puts
the row back and says so, which is the contract the edit path already kept.

A row is not clickable for the one request it takes its real id to arrive, because a
second write against a temporary id would 404 and roll the row off the screen, which
would look like the app eating what was just typed.

Opening Vesta drew an empty shell until `/api/state` answered -- **2.3 seconds** on
that same held connection, because the term is the big query. Two things changed. The
last answer and the preferences that draw it are kept on the device and painted as
soon as the server says who is signed in, address-bar route and all; the fresh copy
folds in behind them and redraws nothing when it matches, which is the usual case.
And the term and the preferences no longer wait for the "who is signed in" answer to
be asked for -- the cookie decides all three, so they go out together. A return visit
is **1,272 ms**, one round trip, and it draws a usable calendar offline.

The device copy is a cache and nothing else: it is only ever painted for the account
it was saved under, signing out removes it, a browser that has never been signed in
does not prefetch, and nothing is ever written back to the server from it.

### The calendar's List view is an agenda

It listed every dated thing in the term, finished work included, which made it a
second copy of the month grid rather than something to work from. It now shows today
onward, plus anything overdue that is not done. Past work that is done is left out.

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

### SQL that runs on SQLite is not SQL that runs on Postgres

Schema is not the only thing the two paths disagree about, and the statement-level
differences are worse, because they are invisible until one particular request is made
in production. Three have bitten, all of them found only from Saif's bug reports:

* **`col IS ?`** is SQLite's null-safe equality. Postgres takes `IS` only before NULL,
  TRUE, FALSE or DISTINCT FROM, so `IS $1` is a syntax error. Branch on the value and
  emit `IS NULL` or `= ?`.
* **The same column assigned twice in one `SET`** is last-one-wins in SQLite and
  `multiple assignments to same column` in Postgres. `PUT /api/materials/<id>` built
  one of these whenever `classId` and `folderId` arrived together, which is exactly
  what dragging a file onto a folder sends, and nothing else.
* **A NUL byte in a text value** stores fine in SQLite; psycopg refuses it outright
  with "PostgreSQL text fields cannot contain NUL (0x00) bytes". `pypdf` returns them
  from some PDFs, so one reading in six would 500 on upload and the rest were fine.
  `db_safe_text()` strips them, and everything that extracts text runs through it.

`tests/test_postgres_dialect.py` guards all three, and runs on SQLite: the point is to
catch the production-only shape without a Postgres to test against. Add to it whenever
a fourth turns up.

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

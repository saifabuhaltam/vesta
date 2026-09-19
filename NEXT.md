# Vesta: what's left

A running list so nothing is lost between sessions. **needs Saif** means it cannot be
done from a terminal: a dashboard login, an email click, or a decision that is his.

_Last updated: 2026-09-14. Semesters are live; the calendar is the next real piece of work._

---

## Deploying

Live at **https://vesta.study**, auto-deploying from `main`. `/health` reports the truth about any
instance: which database, whether accounts are on, the table count, and whether Railway
actually mounted the volume.

Confirmed working on 2026-09-14: `database: postgres`, `accounts: true`, `tables: 30`,
`volumeMountedAt: /data`, `migrations: ["001_semesters"]`.

- [ ] **needs Saif** — Sign in once and start fresh.
- [ ] Smoke-test the live site: sign in, add a class, import a syllabus, connect the
      calendar, upload a file, run a Headstart, export the calendar.

**No data migration.** Saif decided on 2026-09-13 to start from scratch rather than
carry the local database across: he will re-upload the syllabi and let the app rebuild
from those. `cloud/migrate/export_to_pg.py` and the generated `import.sql` still work
and are kept in case that changes, but nothing depends on them. This also makes two
earlier problems moot: the two orphaned syllabus files, and getting `data/uploads/`
onto the Railway volume.

## Deployment gotchas worth writing into the checklist

- **Turning on auto-deploy does not deploy what is already pushed.** It fires on the
  next commit. After enabling it, either push something or use ⌘K → Deploy Latest
  Commit, or the service sits on the old version looking like the setting did not work.
- **"Could not load branches" means Railway has lost its GitHub access**, and auto-deploy
  shows as unavailable while that is true. Fix it at
  github.com/settings/installations → Railway, by confirming the app can see the repo.
  Retry on the Railway screen first; it is sometimes only a stale token.
- **A service can be linked to a template as well as a repo** ("Upstream Repo", with an
  Eject button). That link can fight with ordinary repo deploys. Eject detaches it, and
  is one-way. Never press **Update** on the "new version of the upstream repo" banner:
  it pulls the template's version over the service's own configuration.
- **Do not infer that auto-deploy works because the live version matches a commit.**
  That inference was made here on 2026-09-14 and was wrong: the commit had been
  deployed by hand. `/health` reporting the running commit tells you what is running,
  never how it got there.

- **Railway runs Python 3.13; local development runs 3.9.** Nothing here tests against
  3.13, so a version-specific problem reaches production unseen. Worth either pinning
  the runtime or testing against 3.13 before trusting a release. `datetime.utcnow()`,
  used throughout, is deprecated from 3.12 and will eventually be removed.
- **A stale deploy and a fixed bug look identical from outside.** Confirming the fix was
  live took several rounds of guessing until `/health` started reporting the running
  commit. Check `version` there before debugging anything that "should already be
  fixed".

- **Supabase needs both Site URL and Redirect URLs set**, not just one. Site URL is the
  fallback destination after an OAuth sign-in and ships as `http://localhost:3000`;
  Redirect URLs is the allowlist that `redirect_to` is checked against. Setting only
  the allowlist still dumps the user on localhost:3000, which looks exactly like a
  broken app. Both live under Authentication → URL Configuration.
  Values: `https://vesta-production-8a53.up.railway.app` and
  `https://vesta-production-8a53.up.railway.app/**`.
- **Railway volumes are created from the project canvas** (⌘K, or right-click), not
  from a Settings sub-menu inside the service.
- **`DATA_DIR=/data` and the volume mount path must agree.** Either one alone looks
  like it works and silently loses uploads on the next deploy.

## Semester management (built 2026-09-13, live since 2026-09-14)

Saif's requirement, in his words:

- Add semesters/terms as a first-class part of Vesta (Fall 2026, Spring 2027, Summer 2027).
- When the semester ends, add the ability to archive it.
- Archived courses should remain searchable and readable.
- Starting a new semester should give a clean current workspace without deleting old work.
- Allow switching between current and past semesters.

All five are built. His decisions, made before the work started:

- **The whole app switches**, not just the class list. Dashboard, calendar, files,
  notes, grades, Headstart, quizzes, decks and the .ics export all show one term.
- **Archived terms open read-only**, with an unlock button. The unlock lasts for the
  browser session and drops the moment you switch away.
- **Unfiled notes and files belong to a term** rather than staying global. Worth
  knowing the consequence he accepted: something dropped into the inbox during finals
  week is not visible from the new term until you switch back to the old one.
- **Cumulative GPA was built in the same pass**, since the data was there.

How it works:

- `semesters` is a real table. `classes.semester_id` scopes everything that hangs off
  a class; the six tables that can exist without one (`items`, `events`, `notes`,
  `materials`, `flashcard_decks`, `quizzes`) carry their own copy, kept equal to the
  class's whenever there is a class.
- The active term is a server-side pointer in `app_settings`, not a query parameter.
  Two tabs and two devices therefore agree, and a stale tab cannot ask for a term by
  passing an id.
- Writes to an archived term are refused by the server with `423`, in a
  `before_request` hook. A read-only record that is only read-only in the interface is
  not read-only.
- Search deliberately still spans every term, archived included, and labels any hit
  from another term. That is the "archived courses remain searchable" requirement.
- `/api/history` returns each term's grade material, and the page runs the *same*
  grade functions over it, so an archived GPA and a live one cannot be worked out two
  different ways.

Tested locally: the migration against today's database, against a 22-table backup
from before notes and materials became class-optional, and twice in a row for
idempotence; then the full lifecycle over HTTP (start a term, clean workspace, switch
back, archive, `423` on write, unlock, write, switch away, locked again, refuse to
delete a term with work in it).

### Tested 2026-09-13 (was: "the migration has never run")

It has now, against a real Postgres via `pgserver`, in the two shapes it will meet:
the pre-semester schema with two accounts' worth of coursework in it, and a database
built from the current `pg_schema.sql`. 57 checks pass, plus 33 more driving the API
through a term's whole life, plus a browser pass over the switcher, the archived
banner and unlock.

One real bug came out of it and is fixed in `1dc1e5b`: the migration lifted forced
RLS on the eight tables it backfills but not on `semesters`, which it also inserts
into, so a **brand-new** deployment would fail to boot with *new row violates
row-level security policy*. It stayed invisible because superusers bypass RLS
entirely — the test only found it once the migration was run as a non-superuser.

- [ ] **The test scripts live in the scratchpad, which is session-local and will be
      gone.** This repo has never committed tests. Worth deciding whether
      `test_semester_migration.py` and `test_semester_api.py` should become the first
      ones, because the migration test carries a negative control that is the only
      thing standing between a silent no-op backfill and the live database.
- [ ] **Confirm what role Railway's `DATABASE_URL` connects as.** If it is not a
      superuser, the fix above is load-bearing. Either way the migration is now
      correct; this is worth knowing before the next migration is written.

### How schema changes reach a deployed database

`ensure_pg_schema()` builds the tables once and then returns early forever, so a
deployed database could never gain a column. There is now a migration runner
(`db.run_pg_migrations`) and `cloud/migrate/pg_migrations.sql`, applied under the same
advisory lock, each block in its own transaction, recorded in `schema_migrations` so a
redeploy is a no-op.

It lifts `force row level security` on the tables it backfills and puts it back in the
same transaction, which is still the part worth a second pair of eyes on any future
migration.

- [x] ~~After deploying, check `/health`.~~ Done: live reports
      `migrations: ["001_semesters"]` and `tables: 30`. The backfill ran on the real
      database without incident.
- Note for next time: a failed migration is a failed deploy, because the app raises at
  boot rather than serving a half-migrated database. Deploy a migration when there is
  time to read the log.

### Rough edges left in this feature

- [ ] **Only the Classes screen hides its create buttons while a term is locked.** The
      "add" affordances elsewhere still appear and fail with a clear message from the
      server instead of being disabled up front. Worth tidying, not wrong.
- [ ] **`term_settings` is now unused.** Every reader moved to the active semester. The
      table and its rows are left alone rather than dropped, since nothing is deleted
      here, but it should come out in a later migration once this has been live a while.
- [ ] **Moving a class between terms is not in the interface.** The schema supports it
      and `semester_for` keeps child rows honest, but there is no button. A class
      created in the wrong term has to be deleted and remade.
- [ ] **An archived term still offers "Start Focus Session" and "What to work on".**
      Seen in the browser: the dashboard of a locked term invites you into actions the
      server then refuses with a 423. Reading old coursework should not look like
      working on it.
- [x] ~~The term list grows down the sidebar.~~ Moot: terms left the sidebar entirely
      in `f43a8db`. The sidebar is navigation only and everything term related now lives
      in one Terms section in Settings, which is Saif's standing preference for this app.

## Calendar sync: what Saif asked for, and what actually exists

Saif, 2026-09-13: *"it only updates when i press sync, and it is not two-way. if i add
something to google calendar, it will not populate in vesta. i want it to be in sync at
all times, and i want it to go two-way. that's how i have my notion calendar, and i
expect the same from vesta"*

Where it really stands, checked 2026-09-14:

- **Two-way in mechanism, yes.** `/api/calendar/google/sync` pulls from Google first and
  then pushes, deliberately in that order so a change made on both sides is known before
  anything is overwritten. `gsync.plan_pull` / `plan_push` / `resolve` all exist.
- **Continuous, no.** That route runs only when the Sync button is pressed. There are no
  watch channels, no polling, no cron, and nothing calls it on page load.
- **It only ever touches a calendar Vesta created for itself**, named "Vesta"
  (`gcal.ensure_calendar`). It never reads the calendars Saif already has. This is almost
  certainly why connecting the account appeared to do nothing.
- `gcal.list_calendars()` was written as groundwork for choosing calendars and is wired
  to nothing yet.

Staged plan, in the order that delivers something usable soonest:

- [x] ~~**1. Choose which calendars, and read them.**~~ Shipped in `ee59f75`. A
      `calendar_feeds` table, a picker in Settings, and the pull side reading every
      chosen calendar. Calendars arrive switched **off**: the picker is the consent.
      Chosen calendars are mirrored into `events` as read-only rows tagged "From
      Google", with their own legend toggle; unticking one withdraws its events, which
      is what `events.feed_id` is for. Two pre-existing bugs fell out of it: events
      never exposed `source`/`read_only` to the page, and an unknown event `kind`
      crashed the whole calendar render.
      **Not yet confirmed against the real Google** -- all 24 checks run against a stub,
      which proves Vesta's logic and nothing about Google's behaviour. First real test:
      open Settings on the live site, confirm the calendar list is right, tick one, and
      see whether its events land.
- [x] ~~**2. Sync without a button.**~~ Shipped. The page syncs when it opens, when the
      tab is looked at again, about four seconds after a local change to an item, event
      or class, and on a three-minute timer while it is visible. A 45-second floor means
      however many triggers fire, only one sync happens; failures back off to a
      15-minute ceiling instead of retrying into a wall. A sync that changed nothing does
      not re-render, so the page cannot flicker or eat a half-typed note. Syncing is
      skipped entirely while an archived term is open, because a sync writes and an
      archived term refuses writes: without that it would collect a 423 every few
      minutes. The Sync button stays, since a person who wants to know *now* should be
      able to ask.

      Verified in a browser: an event added to Google while the tab sat idle appeared on
      its own when the tab was returned to; twenty rapid focus events produced zero extra
      syncs; and an archived term produced zero sync attempts.
### 3. Real push — ON THE BACK BURNER (Saif, 2026-09-14)

Deliberately parked, not abandoned. Stages 1 and 2 are live, and polling is expected to
be enough for one student and a few friends. Revisit only if the lag is actually felt.

**What stage 2 does not cover, and would be the reason to do this:**

- Nothing happens while Vesta is closed. Changes queue up until the next time it opens.
- With the tab open but untouched, a change made in Google can take up to three minutes
  to appear, because the timer is the only trigger that fires on its own.
- Nothing pushes *out* of Vesta while it is closed either. A deadline added on a laptop
  does not reach Google Calendar until Vesta is opened somewhere.

**What building it involves:**

- `POST /api/calendar/google/webhook`, one watch channel per enabled feed, registered
  through `client.watch(calendar_id, ...)` against the live HTTPS URL. Google will not
  deliver to localhost, so this cannot be tested locally without a tunnel.
- A `calendar_channels` table, or columns on `calendar_feeds`: channel id, resource id,
  and expiry. Stopping a channel needs both the channel id and the resource id, so
  storing only one leaves an undeletable channel behind.
- A Railway cron to renew before expiry. **Channels last about a week at most and Google
  never renews them.** Miss the renewal and push sync silently stops, which looks exactly
  like the bug this whole section exists to fix, so the renewal job needs to be visible
  in `/health` rather than trusted.

**Traps already known, worth not rediscovering:**

- Notifications are **headers only**. The body is empty: `X-Goog-Channel-ID` and
  `X-Goog-Resource-State` tell you *that* something changed, never what. The handler's
  only sane response is to run the same incremental sync stage 1 already does.
- The first notification after registering is a `sync` ping and means nothing. Acting on
  it as though an event changed causes a pointless full pass.
- A `410 Gone` on the sync token means the cursor is too old and the calendar has to be
  re-read in full. Stage 1 already handles this per feed; push must not bypass it.
- The endpoint is public and unauthenticated by nature. It must do nothing but look up
  the channel id, and it must never trust anything else in the request.
- Google retries aggressively on a non-2xx. The handler should answer 200 immediately
  and do the work after, or a slow sync turns into a stampede.

Decisions already given by Saif: pick which calendars to sync; Google events become
editable Vesta events that sync back; everything dated in Vesta pushes out.

## Missing from the original data plan

These were in Saif's first round of notes and never got built. Recorded so they are
not rediscovered a third time.

- [x] ~~**Semesters as a first-class entity.**~~ Built 2026-09-13, see above. Not deployed.
- [ ] **Separate object storage for uploads.** Uploaded PDFs, videos and images live on
      the Railway volume at `/data/uploads`, referenced by `materials.stored_name`. The
      original plan was cloud object storage instead, which is one machine fewer to
      depend on, plus a CDN and someone else's backups. The R2 work in `cloud/` did
      exactly this and is currently a delete candidate. Consequences of leaving it:
      the volume is a single point of failure, backups are ours, and the 25 MB per-file
      cap in `app.py` stays.
- [ ] **Friends and sharing.** Accounts work, but there is no users table locally to
      hang a display name on, and the row level security policies are strictly
      owner-only. Sharing a class or a note with a friend is not a new join table, it
      is a policy rewrite on all 28 tables. Worth designing before it is promised.
- [x] ~~**File folders.**~~ Built 2026-09-14. `file_folders` is a real tree with
      `parent_id`, matching `note_folders`, and `materials.folder_id` points into it.
      Every class gets Lectures, Readings, Assignments, Rubrics, Exams, Syllabus and
      Personal; the old flat `category` values were migrated to folders of the same
      kind, and `category` is kept only as the filename guess that picks a folder on
      upload.

## Bugs found and not yet fixed

- [x] ~~**Syllabus import shares one file between two tables.**~~ Fixed 2026-09-14.
      `do_import` now copies the bytes to the material's own `stored_name` and files
      it in the class's Syllabus folder, so the import row and the file in Files own
      separate copies and deleting either leaves the other intact. The two already
      orphaned rows (PHIL 110, REM 388) are still orphaned: their bytes were gone
      before the fix, so they need re-importing or deleting by hand.
- [ ] **`extracted_text` is empty for every material.** Re-checked 2026-09-14 and the
      earlier diagnosis was wrong. The four rows are two screenshots, which have no text
      to extract, and the two orphaned syllabus PDFs, whose bytes are gone, so extraction
      had nothing to read in any of the four cases. Extraction itself runs on upload
      (`extract_text`) and on boot (`backfill_extracted_text`) and is probably fine, just
      never exercised on a real document. Partly closed 2026-09-15: pypdf was run over a
      LibreOffice-produced PDF and over a real `.docx` and returned correct text in both
      cases, so the extraction code itself works. Still unverified is a real
      publisher-produced lecture PDF uploaded through the upload route, which is the case
      most likely to return empty text (scanned or image-only PDFs have no text layer at
      all, and pypdf cannot OCR). The real gap is downstream: nothing in the UI searches
      `extracted_text`. Files-page search matches title, filename and class code only, and
      global search indexes file names only. Headstart does read it.
- [ ] **Commit `7b5d88f` has a Python script as its commit message.** My heredoc
      nesting error: the message and the script were swapped. The code in it is correct.
      Fixing means `git commit --amend` plus a force-push, which rewrites pushed
      history, so **needs Saif** to say go.
- [ ] **The sync indicator reads "Connecting…" on the sign-in screen**, before anyone
      has signed in. Cosmetic, but it is the first thing a new user sees.

## Blocked on Saif

- [x] ~~Rotate the Google OAuth client secret.~~ Done by Saif, 2026-09-14. A secret was
      pasted into a chat transcript on 2026-09-13 and had to be treated as compromised.
      Standing rule that outlived it: neither the client ID nor the secret ever needs to
      pass through a conversation. Both go from the Google console straight into the
      Supabase provider page and Railway's variables.
- [ ] **Add the app logo and submit for brand verification.** Deliberately skipped at
      publish time: uploading a logo triggers brand verification, which gates the
      consent screen behind a review (minutes if automated, 2-3 business days if
      manual). Publishing without one shows the domain instead, which is fine. Do it
      later, when nothing depends on the outcome. Note that the logo on the live consent
      screen must match the file submitted, so swapping it afterwards without
      resubmitting puts the app out of compliance. The mark is `static/vesta-mark.png`;
      Google wants a square PNG around 120x120, so check and resize first.
- [ ] **Add `--bind 0.0.0.0:$PORT` to the Procfile.** gunicorn currently picks the port
      up from the `PORT` environment variable, which works, but with `PORT` missing it
      silently falls back to `127.0.0.1:8000`. The app would look healthy in its own
      logs while being unreachable from outside, which is a horrible failure to debug.
- [x] ~~Decide whether to publish the Google app, or stay in Testing.~~ Published
      2026-09-13 on the custom domain, so no more 7-day token resets. Testing mode
      works and allows up to 100 test users, which covers Saif plus friends: each person
      is added under Google Auth Platform → Audience → Test users. The cost is that
      refresh tokens expire every 7 days, so Google Calendar needs reconnecting about
      weekly. Publishing removes that, but requires homepage, privacy policy and terms
      of service URLs plus registered authorized domains, and Google may refuse
      `up.railway.app` since it is not a domain Saif owns. Doing it properly probably
      means a custom domain, which also means re-registering the OAuth callback URL.
      If we go that way: add `/privacy` and `/terms` pages to Vesta first, they are easy.
- [ ] **Google Calendar credentials.** `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
      from a Google Cloud project with the consent screen published to **Production**
      (in Testing, refresh tokens expire every 7 days). The redirect URI must now be
      the Railway domain, not localhost. Placeholders are in `.env`. The API is free.
- [ ] **Prove two accounts are isolated on the live deployment.** The same checks pass
      against a local Postgres, but confirming the deployed database matches needs the
      dashboard and clicking confirmation emails. **Do this before any friend gets a
      login.**
- [x] ~~**Invite friends**~~ Done 2026-09-17. The allowlist is `INVITE_EMAILS` in
      Railway, not SQL in Supabase. Live and verified: `inviteOnly: true`,
      `invitedCount: 3`, and an uninvited signup returns 403 having created nothing.
      `AI_GLOBAL_DAILY_CAP_USD` is set to 5, which matters more than the invite list --
      the per-account cap lives in `app_settings` and every user can raise their own.
      `/health` reports all three so the configuration can be checked from outside.

## Accounts and multi-user: spec checked against the code 2026-09-15

Saif's spec was sign up, log in, log out, reset password, manage profile, plus fully
separate data per account. The isolation half is real and thorough; the account
*management* half is mostly unbuilt on the deployment that actually runs.

**The isolation is genuinely done.** Every one of the 28 app tables in
`cloud/migrate/pg_schema.sql` carries `user_id uuid not null default auth.uid()`
referencing `auth.users` with `on delete cascade`, plus enabled *and forced* row level
security and four owner-only policies. `pgshim.Connection.become()` sets
`request.jwt.claims` and drops the connection to the `authenticated` role for the life
of the request, so the database filters every read and stamps every insert without a
single one of the app's ~262 queries mentioning a user. Classes, notes, files,
assignments, calendar, grades, headstart history and settings are all covered, and so
are the tables the spec did not name: subtasks, quizzes, flashcards, rubrics,
note_versions, sync_links, calendar_accounts, ai_usage. Uploaded bytes share one
directory but are only reachable through `/api/materials/<id>/download`, which looks
the row up through RLS first, so a guessed id returns 404.

### Built 2026-09-15, in the order Saif approved

All of this is local and uncommitted; nothing has been pushed.

- [x] **The background-thread RLS bug.** `make_office_preview` now takes the account it
      belongs to, captured in `queue_office_preview` while a request context still
      exists. `db.for_each_account` runs the three boot backfills once per account for
      the same reason. Proven both ways in `tests/pg`: without the owner the UPDATE
      matches zero rows and the file stays 'pending'; with it, the row changes.
- [x] **The account screen, behind the avatar.** The initial in the top right now opens
      an Account modal rather than Settings: display name, who you are signed in as,
      current/new/confirm password fields, and Sign out, all visible at once. Backed by
      real Flask routes, so none of it depends on the dead `BE` shim any more.
- [x] **Changing a password costs the current one.** `POST /api/auth/password`
      re-authenticates against Supabase to get a token it is allowed to write with,
      which is both the safer design and the only one available: Flask does not keep
      the Supabase token after sign-in.
- [x] **Reset actually resets.** `consumeAuthFragment` now reads `type=recovery` and
      shows a Set-a-new-password screen holding the token, instead of signing the
      person in with the password they just said they could not remember. Same endpoint
      as above, authorised by the recovery token instead of the old password.
- [x] **The invite gate is enforced in Flask**, in `_session_from_token`, which every
      way in funnels through -- including Google, which never touches `/api/auth/signup`
      at all. Reads `INVITE_EMAILS`. Unset means open, on purpose: the alternative locks
      Saif out of his own app on a deploy where the variable is missing. `/api/auth/me`
      and `/health` both report which mode is on, and the sign-in screen only claims
      "Invite only" when it is true.
- [x] **A global AI cap.** `AI_GLOBAL_DAILY_CAP_USD` bounds spend across every account.
      The per-account `daily_cap_usd` was never a spend control: it lives in
      `app_settings` and anyone can raise their own through `PUT /api/ai/settings`.
- [x] **`cloud/` removed** except `cloud/migrate/`, which is load-bearing -- `db.py`
      reads `pg_schema.sql` and `pg_migrations.sql` from it at boot.

### Proven, not assumed

`tests/pg` runs a real Postgres as a NOSUPERUSER role with no BYPASSRLS, which is the
only configuration in which any of this means anything: as a superuser every policy is
bypassed, FORCE included, and the suite would pass while isolated nothing. 48 tests
across both suites. The isolation claims in the audit above are now measured rather
than read off the schema.

### Settings, rebuilt 2026-09-15

Eleven sections down the side of the modal instead of one scrolling column, and the
preferences behind them moved off `localStorage` onto the account. That last part was
the real gap: settings were per *browser*, so signing in on a phone gave you defaults
you had never chosen, and clearing site data wiped them. They live in `app_settings`
now under one JSON blob, which needed no schema change and no new policy because that
table is already per user and already covered by row level security.

Sections: Account & Profile, Semester, Calendar & Integrations, Notifications,
Grades & Grading Scale, Notes & Editor, Files, Focus, Headstart & AI, Appearance,
Data & Export. The avatar opens the screen on Account, which is what makes it the
profile control; there is only one implementation of the password fields.

New behaviour that did not exist before:

- **Notifications.** There were none at all. Browser notifications now fire when a
  focus block ends and when work is due within a window you set. Off by default: a
  permission prompt on first load, for something nobody asked for, is the fastest way
  to be denied permission forever.
- **A default grading scale.** `DEFAULT_GRADE_SCALE` was hard-coded; it is editable
  now, and a class with its own scale still keeps it.
- **`GET /api/export`** returns every row this account owns as one JSON file, across
  29 tables. No user filtering appears in those queries and none is needed: RLS has
  already narrowed each table to the caller.
- Files default layout, Headstart layout, and a sidebar-starts-collapsed default.

Settings write on change rather than behind a Save button, so closing the window
cannot lose four changes at once. `PUT /api/prefs` merges and clamps: an unknown key
is dropped and a focus block of 99999 minutes is stored as 120, so a hand-written
request cannot break the timer permanently.

Deliberately not built: **deleting account data.** Saif ruled account deletion out on
2026-09-15, and wiping every row is the same thing by another name. The Data section
says so and offers it.

### Verified against the live site 2026-09-17

- Uninvited signup: 403, "That email has not been invited to Vesta.", Supabase never
  called. The check used to sit only in `_session_from_token`, which runs after Supabase
  has already made the user -- and never at all when email confirmation is on, so a
  stranger got as far as "check your inbox" before being refused at sign-in later.
- `/api/state`, `/api/prefs`, `/api/export` and `/api/auth/profile` all 401 signed out.
- Password reset returns the identical answer for an address with an account and one
  without, so it cannot be used to find out who has one.
- `rlsEnforced: true` with `connectsAsSuperuser: true`, which is the expected shape on
  Railway. See the note above on why the second one is not a problem and what it does
  mean for any query outside a request context.

Still unverified in a browser: the Settings panes, the account screen, and the
set-a-new-password screen. No browser in the environment that built them.

### Still open

- [x] ~~**Is the live database's role actually subject to RLS?**~~ Answered
      2026-09-15: **yes, accounts are isolated.** The live `/health` reports
      `connectsAsSuperuser: true` -- Railway does hand out a superuser -- but that is
      not the role that matters. Every request calls `set role authenticated` before
      touching a table, and Postgres evaluates row level security against the current
      role, so the policies still apply. Confirmed by experiment against a local
      Postgres configured the same way: with a superuser connection, one account
      inserted a class and the other read an empty table.

      The corollary is the thing to remember. On a host like this, a query that never
      calls `become()` runs as the superuser and reads **every** account's rows. It
      fails open, not closed, which is the opposite of what the schema comments assume.
      `db.get_db()` outside a request context is exactly how that happens -- which is
      what the Office preview bug was, and why that class of bug is worse here than the
      silent no-op it looked like. Treat any new `get_db()` call outside a request as a
      security question, not a correctness one.
- [ ] **The schema's grant block hides its own failure.** `grant authenticated to
      current_user` sits in a DO block with `exception when others then null`, and its
      comment claims "RLS still applies" if it fails. That is wrong: when the grant
      fails, every connection dies at `set role authenticated` and nothing works at all.
      Found the hard way -- a second test run hit exactly this, because `authenticated`
      is cluster-wide and outlived the role that created it. Worth failing loudly.
- [ ] **Three gitignored files under `cloud/` could not be deleted** and are still on
      disk: `cloud/app/config.js`, `cloud/dist/`, and `cloud/setup/secrets.local.txt`.
      The last one is a secrets file and wants a look before it goes. A copy of the
      whole removed stack is archived outside the repo.
- [ ] **The account and reset screens have not been opened in a browser.** The
      JavaScript parses and the routes behind it are tested, but no one has clicked
      them. There is no node and no browser in this environment.
- [ ] **Session length is still 30 days**, so removing someone from `INVITE_EMAILS`
      does not sign them out until their cookie expires. The list is checked at sign-in,
      not per request.

## Headstart: three questions Saif asked 2026-09-14, checked against the code

### 1. Where generated Headstart output goes (today: not to Files)

Nothing Headstart generates ever becomes a file. There is no `materials` row, no
folder, no download. What actually happens:

- **Five of the ten tools save a row on the assignment** (`headstarts` table, unique
  per `item_id` + `kind`, written in `ai.py` `ai_run`): outline, explain, draft,
  study plan, summarise. These show in the assignment card's Headstart tab.
- **The other five are transient**: rubric breakdown, concepts, gaps, revise, refine.
  `ai_run` returns the text and writes nothing. Close the panel and it is gone. This
  is a bug, not a decision.
- **The only ways to keep any of it** are the two buttons on the result bar: Copy, and
  "Save as a note" (`hsSaveAsNote`, `index.html` around line 9034), which creates an
  ordinary note in the class.

- [ ] **needs Saif** — decide where generated work lives. Three options:
      (a) notes only, and fix the five transient tools to auto-save onto the assignment;
      (b) a real `Headstart` folder per class in the Files tree, each run written as a
      file, which makes it searchable, previewable and downloadable alongside the
      source material but puts generated text in a tree built for uploads;
      (c) both: auto-save every run onto the assignment, plus a "Save as a file"
      button next to Copy and "Save as a note".
      Recommendation is (c). It is the most work, but it is the only one where
      "where did that outline go" has an answer that does not depend on which of the
      ten tools produced it.

### 2. What Headstart can actually read

It reads assignment descriptions, attached files, class files and notes, but with four
limits worth knowing:

- **The assignment description is read.** `items.notes` goes into the prompt as
  "What the student recorded about it" (`assignment_brief`, `ai.py`). Typing what a
  quiz covers onto the assignment does reach the model.
- **Files attached to the assignment are read in full**, all of them, before anything
  else (`item_files` join in `collect_sources`).
- **Class-wide material is capped at the 5 newest files and 5 newest notes** when no
  sources are picked by hand. Lecture slides from week 2 will not be read in auto mode
  by week 10. The source picker (`/api/ai/context/<cid>`) lists every file, note,
  folder and syllabus topic, so picking them explicitly lifts the cap.
- **Each source is truncated to 6,000 characters**, 60,000 across the whole prompt
  (`PER_SOURCE_CHARS`, `TOTAL_CONTEXT_CHARS`). A full lecture PDF contributes roughly
  its first few pages, silently.
- [x] ~~**PowerPoint contributes nothing.**~~ Fixed 2026-09-15. The PDF reader is now
      `extract_pdf_text`, shared by uploads and by Office conversions, and
      `make_office_preview` runs it over the converted PDF once LibreOffice finishes.
      Every format in `OFFICE_EXTS` is now readable: `.pptx`, `.ppt`, `.odp`, `.xls`,
      `.xlsx`, `.ods`, `.doc`, `.rtf`, `.odt`. Two details worth keeping in mind:
      it only fills an **empty** `extracted_text`, so `.docx` keeps python-docx's much
      better output (verified: 25,983 characters from the IAT 201 course map, unchanged
      after a conversion ran over it), and `backfill_office_text()` at startup reads
      decks that were converted before this existed, using the PDFs already on disk
      rather than re-running LibreOffice.
      Verified end to end on a real `.pptx`: `extract_text` returned `None` before the
      change and the slide text afterwards, and the backfill refilled a cleared column.
- [ ] **The rubric only reaches Headstart if it was linked from the class Files page.**
      Already recorded under the assignments gaps above; repeated here because it is
      the same class of problem: context that looks attached in the UI but is not in
      the query.

### 3. Chatting with it

**There is no chat today.** `call_claude` sends a single user message with no history
(`messages=[{"role": "user", ...}]`), and the frontend keeps no transcript. The only
steering is the "Additional instructions" box, capped at 4,000 characters, and
"Run again", which re-runs from scratch rather than continuing. So "make that shorter",
"focus on chapter 3" or "why did you say that" are not possible.

It is buildable, and the cost objection turns out not to hold once prompt caching is
used. Costed on Sonnet 5 ($2/$10 per MTok, Vesta's configured default) with a full
60,000-character context (about 15,000 tokens):

- **Without caching**, every turn re-sends the whole course context: about $0.03 per
  turn in input alone, rising as the transcript grows. A ten-turn conversation runs
  $0.35 or more, which is a third of the $1.00 daily cap for one chat.
- **With caching** (`cache_control` on the context prefix), the first turn pays a 1.25x
  write and every later turn reads that prefix at 0.1x: roughly $0.0375 once, then
  $0.003 per turn. The same ten-turn conversation costs about $0.10.

Caching is what makes it affordable, and it fits Vesta's shape well, since the expensive
part of the prompt (files, notes, rubric) is exactly the part that does not change
between turns. Two details that matter: the cached prefix must be assembled
byte-identically each turn or it silently misses, and the default 5-minute TTL refreshes
on every read, so a continuous conversation stays warm while a 20-minute gap pays one
fresh write. Verify with `usage.cache_read_input_tokens`, which Vesta does not currently
record (`record_usage` stores input and output tokens only).

- [ ] **needs Saif** — decide whether to build it. Recommended shape: keep the ten tool
      buttons exactly as they are, and make a run open a **thread** rather than a dead
      result pane, with the tool's output as the first assistant message and a reply box
      under it. That keeps the one-click start (the tools are good precisely because you
      do not have to know what to ask), adds the follow-up, and resolves the "where does
      output go" problem above for free, because every run becomes a saved thread instead
      of five saved kinds and five thrown away.
- [ ] The work, honestly scoped: two new tables (`chat_threads`, `chat_messages`, with
      `headstart_sources` reused per thread), a `/api/ai/chat` endpoint that assembles
      cached-prefix + history, the reply UI, and **streaming**. Streaming is the hidden
      cost: `call_claude` is non-streaming, and a chat that shows nothing for 30 seconds
      reads as broken, so this means an SSE endpoint on Flask plus incremental rendering
      on the frontend. It is the largest single piece of the job.
- [ ] Cost-gate behaviour needs a rethink for chat. `confirm_over_usd` defaults to $0.05
      and interrupts any call above it. Per-turn chat costs sit well under that, but the
      first turn of a big context can trip it, and a modal in the middle of a
      conversation is the wrong shape. Probably: confirm once when a thread opens, not
      per turn.
- [ ] Record `cache_read_input_tokens` and `cache_creation_input_tokens` in `ai_usage`.
      Without them there is no way to tell whether caching is working, and a caching
      regression is silent: requests keep succeeding and the bill is just higher.

## Headstart screens: reviewed against real screenshots 2026-09-16

Every Headstart surface was opened in a real browser against a seeded database and
photographed. Screenshots and the harness that produced them are in
`reference/headstart-screens/` (gitignored, like the rest of the design material):
`seed_demo_data.py` fills a throwaway database with three classes, six assignments, a
quiz, a practice test and two decks, and `shoot.py` drives Chrome through the screens.
Run the app with `SUPABASE_URL=` and `SUPABASE_ANON_KEY=` empty and accounts switch off,
which is what makes local screenshotting possible at all.

**There are no Headstart mockups.** `vesta mockups/` covers overview, classes,
assignments, calendar, files, grades, notes and the assignment card, plus pomodoro
inspiration. Nothing for Headstart, the quiz maker, the practice test or the decks, so
none of these screens has a reference to be checked against. They were designed directly
in code. Worth knowing before asking whether they "match the mockups": there is nothing
to match.

Note also that the screenshot workflow described in the ARIA project's `DESIGN.md` has
never worked on this machine. It points at `C:/Users/nateh/AppData/...` for Puppeteer and
a `serve.mjs` that does not exist in this repo, and node is not installed. It is a
Windows setup that was copied in from somewhere else.

### What is good

The workspace (`31-workspace-opened.png`) is the strongest screen in the app. Tools are
grouped by intent (Understand it / Plan it / Work on it), each carries a one-line
description of what it does, and the cost estimate sits next to the Run button before
anything is spent. The card review loop, the quiz player and the three makers are all
visually consistent with the rest of Vesta.

### Bugs and design problems found

- [ ] **Run is enabled when there is nothing to read, and says so.** In the workspace the
      button reads `Run` with `About $0.030 · reads 0 sources` beside it, and the panel
      above says "Nothing selected yet." The interface has already computed that it has no
      input and still offers to spend money on it. Same shape in the quiz maker
      (`11-maker-quiz.png`): "This class has no files or notes yet" sits directly above a
      fully enabled **Build it**. Since the quiz prompt says "Base every question on the
      material provided. Do not invent facts that are not in it", a run with no sources
      either refuses or invents, and is billed either way. Disable the primary action at
      zero sources and say why.
- [ ] **A practice test cannot be started from the Headstart tab.** The per-class row
      there renders only `hs-new-cards` and `hs-new-quiz` (`index.html` around line 4296).
      All three appear on an assignment's own Headstart tab (around line 8959). So of the
      three generators, the hub offers two.
- [ ] **An assignment's Headstart tab says "Nothing generated for this assignment yet"
      while a practice test for it exists.** `itemCardHeadstartHtml` reads `it.headstarts`
      only, so quizzes and decks carrying that `item_id` are invisible there, even though
      the Headstart tab lists them by name one screen away
      (`41-assignment-headstart-tab.png`). This is the "where did my generated work go"
      problem from 2026-09-14 showing up in a second place.
- [ ] **The deck screen is a wall of red.** Every card row carries a red **Delete** at the
      same weight as **Edit**, plus **Delete deck** below (`04-saved-deck.png`). Four cards
      means five destructive controls on one small modal; a realistic 40-card deck means
      41. Destructive actions should not be the most repeated visual element on a study
      screen.
- [x] ~~**Flashcard review is mouse-only.**~~ Fixed 2026-09-16 at Saif's request.
      `fcHandleKey` runs ahead of the global `keydown` handler and owns the review loop:
      space, enter, right or down reveals the card; left and right then move a visible
      selection across the four grades; space or enter commits it; 1-4 pick one directly.
      Up and down are swallowed while reviewing so a held arrow does not scroll the modal
      underneath. The handler ignores everything when the target is an input, a textarea
      or a contenteditable, so typing into a card is unaffected.
- [x] ~~**"Easy" is styled as the primary action on the grading row.**~~ Fixed in the same
      pass. The grade row is now generated from `FC_GRADES`, the same list the keyboard
      reads, so the buttons and the shortcuts cannot drift apart. Nothing is primary-styled
      any more; the armed choice is outlined instead, and it defaults to **Good**
      (`FC_DEFAULT_PICK`) rather than Easy. Each button carries its number badge, so the
      shortcuts are visible rather than hidden knowledge.

### Browser dialogs replaced with Vesta's own, 2026-09-16

Saif asked for the error popups to look like the rest of the app rather than like the
browser. Every native dialog is gone: `grep` for `window.alert(`, `window.confirm(` and
`window.prompt(` in `static/index.html` now returns nothing.

- **A layer of its own.** Dialogs render into a new `#dialog-root` that sits above
  `#modal-root` at a higher `z-index`. This is not cosmetic: modals render by replacing
  `#modal-root`'s `innerHTML` wholesale, so an error raised from inside a modal would have
  deleted the modal that raised it. A confirmation or an error now appears over an open
  modal and leaves it intact, which is verified by a test.
- **`reportError` kept its signature**, so all 112 call sites are untouched. It just draws
  a Vesta panel now instead of calling `alert`.
- **`confirmAction` and the prompts had to become asynchronous**, because a styled dialog
  cannot block the thread the way `confirm` and `prompt` did. All 16 confirm sites were
  rewritten from `if(confirmAction(...)){ ... }` to `.then(function(ok){ if(!ok) return; ... })`,
  and the 8 prompt sites to `askForText` / `askForFields`.
- **`askForFields` shows several inputs in one dialog.** Adding a flashcard used to be two
  prompts in a row, and inserting a table two more; each is now a single dialog with both
  fields, which is also why the prompt conversion did not just become a chain of popups.
- **Keyboard:** Escape cancels, Enter confirms, and while a dialog is open no other key
  reaches the page. An error or notice treats Escape as acknowledgement; a confirm or an
  input treats it as cancel.
- **One at a time.** Opening a second dialog resolves the first as a cancel rather than
  stacking, so a burst of failed requests cannot bury the screen.
- **Not done:** `notify` is used for the three former information alerts (saved to notes,
  copied, no class yet). A toast would suit those better than a dialog that must be
  dismissed, but that is a new component and was not part of what Saif asked for.

Tested with Playwright against a seeded database: 18 checks covering the keyboard review
loop end to end (flip, arrow selection, clamping at both edges, enter committing and
advancing, number keys, the next card starting face down) and the dialogs (fields render,
Escape closes, the modal underneath survives, a confirm opens over a modal, cancel is
non-destructive, a genuinely failed request raises an error dialog carrying the server's
message, Enter dismisses it). Screenshots `51-` to `56-` in `reference/headstart-screens/`.

### Three bugs Saif reported 2026-09-17, all fixed

**Saved generated work would not open.** Reported as "the saved generated stuff doesn't
actually open", and it was worse than it looked: neither route worked.

- `/api/state` serialised each item's headstarts as `{kind, status}` only. No `id`. So
  `hsEarlierHtml` rendered `data-id="undefined"`, and `hs-view-saved` looked that up
  against rows that had no `id` at all, found nothing, and returned silently. Nothing
  logged, nothing flashed; the click simply did nothing.
- The assignment card's saved rows were worse still: they carried
  `data-action="hs-open-item"`, which opens the generic workspace for that assignment.
  Even with a working id they would never have opened the result that was clicked.
- Fixed on both sides. `serialize_item` now selects `id, kind, status, updated_at` and
  sends `id` and `updatedAt`, ordered newest first. The text deliberately does **not**
  travel in `/api/state` — every item's every generation on every state load is a lot of
  payload for something rarely opened — so the new `hsShowSaved(id)` fetches the row from
  the existing `GET /api/items/<id>/headstarts`, shows a working state while it loads,
  and reports a real error if the row has gone. The card rows now use `hs-open-saved`,
  which opens the workspace *on* that result and selects the tool that produced it
  (`toolKeyForKind`). They also say "saved yesterday" rather than "saved earlier", since
  `updatedAt` is now available.

**Answering a quiz threw you back to the top.** `renderQuiz` rebuilds `#modal-root`
wholesale on every answer, so `.modal-body` lost its scroll position each time. By
question 8 you were scrolling back down after every click. `renderQuiz` now remembers
`scrollTop` before the rebuild and restores it after, reading a layout property first so
the assignment is not clamped to 0 on an element that has not been laid out yet. Measured
at 0px drift across five consecutive answers.

**Written answers were silently discarded.** Found while fixing the scroll. The textarea
for short-answer and concept questions carried `data-action="qz-write"`, and *nothing in
the codebase read it* — one occurrence in the whole file, in the render. `QZ.answers` was
therefore never set for those questions and `qzSubmit` posted an empty value for every
one. You could type a full answer, submit, and be marked as having left it blank. The
global `input` listener now stores the text as it is typed. It deliberately does not
re-render, which would take the caret with it; the answered counter is updated on its own
through `qzPaintProgress`.

Verified with Playwright, 10 checks: both saved results open from both routes and show
their text, the quiz holds its scroll position, typing updates the counter, the caret
stays put, and a typed answer survives submission and marking. Screenshots `64-` to `66-`.

**A note on the testing itself.** Two earlier runs of this suite reported the scroll fix
as working and then as broken, and both were wrong: clicking a choice that is scrolled out
of view makes the browser scroll to it before the click lands, so the test was moving the
page and then blaming the app. Any future test that clicks inside a scrolling container
has to pick an element already on screen. `verify.py` now does.

### Files page filter bar, reorganised 2026-09-17

Saif's words: "I want the sort by type and time to be better organized, i don't like the
cluttered look of it right now." The clutter was structural — eleven borderless, background-
less buttons floating loose across two full-width rows, with the two selected ones tinted
blue, so the eye saw eleven separate things and two unrelated blue blobs.

Folding them into dropdowns was not an option: the rule that every filter stays on screen
is deliberate, and `filesFiltersHtml` already carried the comment "Always on screen, never
folded into a menu." So the grouping is purely visual and nothing was hidden. He chose the
one-row option from three presented.

- **Two labelled segmented groups on a single row.** `Type` and `When` each get a small
  uppercase label and a grey track (`.ff-group` / `.ff-label` / `.ff-track`), built the same
  way as the `.seg-tabs` above them so the page reads as one system. Eleven loose chips
  become two controls, and a full row of vertical height comes back.
- **The selected option is a raised pill** rather than a tinted one, which reads as a
  position within a control instead of a stray highlight.
- **Option labels shortened** now the group is titled: "All types" to "All", "Documents"
  to "Docs", "Past week" to "Week". The row reads "Type: All, PDF, Docs…".
- **Below 820px the two groups stack**, each keeping its label, so the labels line up.
- **Dark mode needed its own rule.** `--surface` is *darker* than `--surface-2` on a dark
  page, so the raised pill would have read as a hole and at those values was nearly
  invisible (track `#24252F` against pill `#1E1F27`). The active pill steps up to
  `--surface-3` in dark and drops the shadow.
- The new styles are scoped to `.ff-track .asg-filter`, so the assignments page and the
  focus settings, which share the `.asg-filter` class, are untouched — verified.

- [x] ~~**The Slides and Sheets filters could never match anything.**~~ Found while
      testing the above, and it is the more serious half of this change. `MIME_LABEL` is
      first-match-wins and tested `/word|document|docx?/` second. Every Office Open XML
      type contains the string **"officedocument"** — a `.pptx` is
      `application/vnd.openxmlformats-officedocument.presentationml.presentation` — so
      every slide deck and every spreadsheet was labelled "Document". Filtering by Slides
      returned nothing on a library full of lecture decks, and the file card's own badge
      said PPTX at the same time, because that comes from the extension rather than from
      this table. The table is reordered so specific formats are tested before general
      ones, with Document near the end, and `.odp`, `.ods`, `.odt`, `.rtf` and `.svg` added
      while there. Checked against ten formats including the older `.ppt` / `.xls` / `.doc`
      MIME types: all correct. In the app, Slides went from 0 files to 1 on the same data.

Screenshots in `reference/files-page/`: wide, narrow, dark, PDF-filtered, Slides-filtered,
and the assignments page for comparison.

## Threads: continuing conversations, built 2026-09-17

Saif's complaint: four discussion posts in one week meant attaching the same readings
four times, each run its own dead end, "compared to a claude or chatgpt chat history
where i can just go back in and ask it to answer discussion 2 based on other material
and it feels continuous".

A telling detail found on the way in: `headstart_sources` has always recorded what every
run read, and **nothing has ever read it back**. The data needed to stop re-attaching was
being written and thrown away.

### What was built

- **`threads`, `thread_messages`, `thread_sources`** in `db.py`. A thread belongs to a
  class, is named by the student, and pins its own sources. Registered in
  `SEMESTER_SCOPED` and in `prefs.py`'s `EXPORT_TABLES`, so threads travel with a term
  and with an export.
- **`threads.py`**, a new blueprint: list, create, read, rename, re-pin, delete, and
  `POST /api/threads/<id>/messages` for a turn.
- **`call_claude_chat` in `ai.py`**, which is where the cost model lives. The pinned
  material goes in `system` with a cache breakpoint after it; the conversation grows
  after that breakpoint. Render order is tools, system, messages, so every turn after
  the first reads the expensive half of the prompt from cache. A test asserts the cached
  prefix is **byte-identical between turns**, which is the invariant the whole $0.10-per-
  ten-turns figure depends on; a single stray timestamp in that block would silently
  triple the bill.
- **`ai_usage` now records `cache_read_tokens` and `cache_write_tokens`**, with an
  `ALTER TABLE` migration for existing databases. Without these a caching regression is
  invisible: the requests still succeed, the bill just goes up.
- **UI**: each class strip gains "New thread" plus its three most recent threads and a
  "N more" list. A thread opens as a transcript with a composer, the pinned sources
  along the top, and the ten tools offered as openers on an empty thread only. Enter
  sends, shift-enter is a newline. A reply can be copied or saved as a note.

### Decisions worth remembering

- **No streaming, deliberately.** I had called streaming the expensive blocker. That was
  wrong: Headstart was already non-streaming and in use that way, so a thread reply
  reuses the same "this can take a minute" state. That turned a staged build into one
  build. Streaming is a later polish item, not a prerequisite.
- **A refusal does not eat the question.** The user's turn is written before the model is
  called, so hitting the daily cap or the confirm threshold leaves what they typed in the
  thread rather than discarding it. Tested.
- **Pinned but unreadable sources are shown, not dropped.** `collect_sources` only
  returns what it could read, so a pinned scanned PDF simply vanished from the list —
  tick three files, see one, with nothing saying why. The thread now lists everything
  pinned, strikes through what has no extractable text, and says so underneath.

### Tested

30 backend checks with the Anthropic call stubbed (creation, pinning, tool-seeded first
turn, history replayed in order, the byte-identical cached prefix, cache tokens recorded,
re-pinning, a failed call keeping the question, cascade delete) and 20 through the real
interface with Playwright, walking Saif's exact scenario: pin once, draft discussion 1
from a tool, ask for discussion 2 in the same thread without re-attaching anything, ask
for it shorter, close, reopen, and find the whole conversation and its sources still
there. Harness and screenshots in `reference/threads/`.

- [ ] **Never run against the live API.** Every test so far stubs `anthropic.Anthropic`,
      so the assembly, the caching structure and the persistence are proven but the real
      request has never been sent. One small live call would settle it; it costs a cent
      or two of Saif's key and has not been done without asking.
- [ ] **The Headstart page is now denser**, which Saif already considered cluttered before
      this landed. He has asked for a layout rework as a separate piece of work, using the
      `frontend-design` skill, keeping the current visual language (assignment cards,
      colours, the bubble style) and accounting for friends arriving as new users. Threads
      were built in the existing language with the smallest footprint on purpose; the
      rework is where the page gets reorganised.
- [ ] Existing saved Headstarts are **not** migrated into threads yet. They still open the
      way they did. Worth doing so there is one place generated work lives, not two.
- [ ] A thread cannot yet be started from an assignment card, only from the class strip.
      `threads.item_id` exists for it and the server accepts it; the button does not exist.

## Headstart rework, first pass 2026-09-17

Saif: "this whole headstart page needs to be reworked... It's like 20% of the screen for
something supposed to be important." Two decisions taken with him before building: work
screens replace modals, and the two doors to the ten tools become one.

Done:

- **A thread is a screen.** Headstart has an inner screen (`hsScreen`), so home is for
  browsing and a thread takes the page: back link, a rail of the class's other threads,
  and the conversation at 50% of the window rather than 20%.
- **Home is three labelled rows** per class, Due / Threads / Study, instead of one flat
  strip that mixed four kinds of object at the same weight.
- **One door to the tools.** "Work on class material" and an assignment's Headstart
  button both started a run that was discarded on close. Both open a conversation now.
  `openHeadstartWorkspace` stays only because older saved runs still open in it.
- **The Reads bar collapses** to one line, opening on hover and pinning open on click.

Still modals, and the obvious next pass:

- [ ] **The quiz player and the deck review are still modals.** A quiz you are sitting
      an exam-practice run of has the same cramped panel the thread used to. They should
      become screens the same way; `hsScreen` already has room for `{kind:'quiz'}` and
      `{kind:'deck'}`.
- [ ] **The makers are still modals** (Quiz me, Make flashcards). Those are short
      configuration steps, so a modal may be right for them. Worth deciding rather than
      inheriting.
- [ ] **No deep links.** `hsScreen` is in memory, so a thread cannot be linked to or
      reopened by URL, and the browser back button does not walk the screens. That is
      the next structural thing, and it wants a decision about routing before more
      screens are added.
- [ ] **Existing saved Headstarts still are not threads.** They open in the old
      workspace. Migrating them would finish collapsing the two surfaces into one.

## Humanizer in Study, built 2026-09-18

Rewrites AI-sounding prose and marks each habit it removed on the original. The prompt
is blader/humanizer's `SKILL.md` (MIT), vendored in `vendor/humanizer/` with the commit
it came from; `humanizer.py` sends it as a cached system block and asks for JSON.
Saif's choices: rewrite plus marked habits, a saved voice sample edited on the screen,
all four sources (paste, note, thread, file), and a kept history.

Measured against the real model on 2026-09-18, Sonnet 5:
- Medium effort: 250 words in 20 s, 1,305 words in 63 s. The default effort took 39 s for
  250 words and was no better; low left inflated phrases in; thinking off dropped a fact.
- Cost is about 4 cents for 250 words and 8 cents for 1,300. The 7,500-token prompt is a
  cache read on a second pass within five minutes.
- The ceiling is 1,500 words, set by gunicorn's 120 s timeout, not the model.

Rough edges left:
- [ ] **Longer papers go through by hand, a section at a time.** Splitting on paragraph
      boundaries and running the parts one request each would lift the ceiling without
      touching the timeout.
- [ ] **No "from Files" source.** Uploaded course files already have `extracted_text`,
      so a picker over them would be cheap. Upload covers it for now.
- [ ] **A note brought in loses its formatting**, and "Replace the note's text" writes
      plain paragraphs back. The confirm says so, and the version history keeps the old
      text.

## Decisions waiting on Saif

- [ ] **Delete `cloud/`?** The Worker, R2 integration and four JavaScript shims are
      unused by the Railway architecture. Committed in `fd2db5a`, so deleting is safe
      and reversible. The row level security work already lives in
      `cloud/migrate/gen_pg_schema.py`.
- [ ] **Google Calendar sync**: written and tested, but its transport has never
      executed against Google. Decide whether to finish it or park it.
- [ ] **Amend the bad commit message** on `7b5d88f`, which needs a force-push.
- [ ] **Settings: account features in the mockup that do not exist yet.** The Settings
      redesign (2026-09-18) follows `reference/settings inspo.png` but leaves out
      delete account, two-factor sign-in, profile photo, and a list of other signed-in
      devices, because none of them are built. Say which, if any, are worth building.
- [ ] **Syllabus supplement: new items land with no category.** A document added later
      to an existing class (2026-09-18) can add new assignments and fill blank dates,
      but it never touches grading, so a brand-new item from a schedule-only document
      arrives uncategorised. Add a category picker on new rows in the supplement review,
      defaulting to an existing category whose name matches the item's type.
- [ ] **Old headstarts vs threads.** The Study "saved" badge still counts the legacy
      `headstarts` table. Decide: migrate those rows into threads, or repoint the badge.

## Documentation (deferred by Saif, 2026-09-13)

- [ ] `README.md` still documents SQLite-only, single-user, and `localhost:5000`.
      Needs Postgres, accounts, Railway, and the real deployment steps. It now also
      predates semesters, which are a user-facing feature it says nothing about.
- [ ] `DESIGN.md` still describes Supabase-plus-Cloudflare. Needs rewriting for
      Railway, with Supabase used only to issue tokens.
- [ ] A deployment checklist matching what was actually built, including checking
      `/health` for `migrations` after any deploy that carries a schema change.

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

## Assignments: Saif's spec, checked against the code 2026-09-14

He restated the requirement in nine points. Seven are built and were verified by reading
the code, not by trusting the design notes.

- All Assignments and By Class both exist (`asgTableHtml`, `asgByClassHtml`), with
  collapsible per-class groups and Expand all / Collapse all.
- All six filters exist (`asgMatchesFilter`): All, Today, This Week, Upcoming, Overdue,
  Completed.
- Due date, type, status, weight and grade are all columns in both views, and weight and
  grade are editable inline (`inlineNumCell`).
- One assignment experience everywhere: Dashboard, the Calendar month, week and list
  views, the Assignments table, the class page's Assignments and Grades tabs, Grades →
  By Assignment, and global search all fire `open-item-detail`, which is the single
  `openItemDetailModal`.
- Files attach through the assignment (upload, drag and drop, or Attach existing across
  every class and the Inbox) and are the same rows as global Files: `item_files` is a
  many-to-many, and the file's home stays `materials.class_id`.
- Notes attach through the assignment (`notes.linked_item_id`) and stay ordinary notes
  in the Notes workspace.
- Headstart sessions belong to the assignment (`headstarts.item_id`, unique per kind),
  and the assignment card has its own Headstart tab.

### Shipped 2026-09-15, and the trap it walked into

The new / edit assignment card went out with the whole working tree in `83e5809`: the
file-folders feature, Office-to-PDF previews and the LibreOffice build change as well.

`83e5809` was broken on Postgres and the mistake is worth remembering, because it is
structural rather than careless. **`init_db()` returns early when `DATABASE_URL` is set.**
Everything below that early return is the SQLite story: the whole ALTER-by-ALTER
migration chain, including `migrate_file_folders`, which creates `file_folders` and adds
`materials.folder_id`, `preview_name` and `preview_status`. A deployed database never
runs a line of it. So a feature can be fully tested locally, pass every local check, and
still reach production with none of its schema, and the failure is not at boot — it is
`relation "file_folders" does not exist` on the first `/api/state`, which is every page
load.

`142d82c` adds `003_file_folders` to `cloud/migrate/pg_migrations.sql`, which is the
only route a schema change has to a deployed database.

- [ ] **Any SQLite ALTER needs a partner block in `pg_migrations.sql` on the same day.**
      This is the second time the two paths have diverged. Worth a check at the top of
      the deploy checklist, or better, a test that diffs the two schemas and fails.
- [ ] **`cloud/migrate/pg_schema.sql` is stale** — it has no `file_folders`. Harmless
      today, because `run_pg_migrations()` runs straight after `ensure_pg_schema()` and
      003 is `create ... if not exists`, so a brand-new database is corrected a second
      later. Regenerating it means running `gen_pg_schema.py` against a *current* local
      SQLite database, and the copy in `data/` may not be one.
- [ ] **No folder backfill on Postgres.** Classes that existed before this deploy have
      no default folders until something is uploaded into them, because
      `folder_id_for_kind` creates them on demand. Files keep their `category` either
      way, so nothing is lost or hidden.

### The gaps, in the order they are worth fixing

- [ ] **Rubrics cannot be reached from an assignment.** The Rubric button only appears
      on the class page's Files list, and only on a file whose category is `rubrics`
      (`index.html` around the material row). Parsing writes `rubrics.material_id` with
      `item_id` left null; the assignment is chosen afterwards from a dropdown inside
      that modal. Consequence worth knowing: Headstart reads the rubric with
      `SELECT * FROM rubrics WHERE item_id=?`, so "Break down the rubric" runs with no
      rubric unless that class-page step was done. Saif's spec says rubrics attach
      *through the assignment*, so this needs an affordance on the assignment card.
- [x] ~~**A classless assignment cannot take a file.**~~ Fixed 2026-09-14.
      `uploadFilesToItem` posts to `/api/materials` when the assignment has no class,
      so the file lands in the Inbox and is still attached to the assignment.
- [ ] **A classless assignment cannot start a note either.** The New note button is only
      rendered when the assignment has a class. Attach existing still works.
- [ ] **Two different note-to-assignment links exist, and the assignment card only reads
      one.** From the note side, the Links menu writes `note_links`, which is a real
      many-to-many: one note can name several assignments, files, lectures and events.
      From the assignment side, the card's Notes tab reads only `notes.linked_item_id`,
      a single column. So a note linked to two assignments through the Links menu shows
      up on neither card. The card should read `note_links` as well, or the two should
      become one mechanism.
- [ ] **New note from the card does not open the note.** It creates an empty note and
      re-renders the assignment card, so the next click is always Open.
- [ ] **The assignment card's tabs are Details / Headstart / Notes.** Files live inside
      Details rather than getting a tab of their own, which is worth revisiting now that
      rubrics are meant to live there too.
- [ ] **"+ Add assignment" is not disabled in an archived term.** Same rough edge as the
      rest of the app: only Classes hides its create buttons, everything else fails with
      the server's 423.

## Files system: Saif's spec, checked against the code 2026-09-14

He restated the Files requirement in sixteen points. Seven were already built and were
verified by reading the code.

Already built:
- Auto-organization by class (`materials.class_id`, plus an Inbox for unfiled files),
  with filename rules assigning a category on upload (`guess_file_category`).
- Drag-and-drop uploading on the Files page (anywhere on the page, not just the
  rectangle), class library sections, the assignment card, and syllabus import.
  Multiple files at once works.
- A drag-and-drop Files area in the assignment popup, with browse and "Attach existing"
  reaching across every class and the Inbox.
- One file, many locations, no duplication: `item_files` is a real many-to-many, the
  file's home stays `materials.class_id`, and global Files reads the same rows.
- Headstart file selection (`materialIds` in the source picker, recorded in
  `headstart_sources`), reading `extracted_text`.
- PDF and image preview inside the app (`openFilePreviewModal`, iframe and img).
- Metadata: class, type, size, upload date, and related assignment ("Used in" chips
  plus auto-suggested assignments).

### Decisions taken 2026-09-14

- **Nested folders**, not flat. A real `file_folders` tree with `parent_id`, the same
  shape as `note_folders`, so files and notes finally behave the same way. Every class
  gets a default set (Lectures, Readings, Assignments, Rubrics, Exams, Syllabus,
  Personal) which can be renamed, nested, added to and deleted. The existing flat
  `materials.category` values migrate to folders of the same name. Auto-filing on
  upload is kept: the filename rules now pick a folder rather than a category.
- **Dragging a file moves it.** Its home class and folder change; its assignment
  attachments survive the move. No second class-membership table, so "where does this
  file live" keeps exactly one answer.
- **Search is metadata and filters only** for now: name, class, folder, type and upload
  date, with visible filter controls. Content search is deferred, see below.
- **LibreOffice for Office previews.** Word, PowerPoint and Excel convert to PDF once on
  upload and the result is cached, so the cost is per file rather than per view. Saif
  accepted the roughly 500 MB added to the Railway image and the per-conversion usage
  cost after both were spelled out.

### Built 2026-09-14

- Nested folders per class, with a New folder button at every level, rename and
  delete from a folder's own dialog. Deleting a folder moves its files and
  subfolders up one level and never deletes a file.
- Drag to move, everywhere a file or a folder is shown: file tiles, list rows and the
  rows inside a class's Files tab are draggable, and class cards, folder cards,
  breadcrumbs and the folder sections in a class all light up as drop targets. A
  folder refuses to be dropped inside itself or its own child, on the client and
  again on the server.
- Uploads land in the folder you are standing in, or in the folder the filename
  implies when you are not standing in one.
- Filters that stay on screen: type (PDF, Documents, Slides, Sheets, Images, Links)
  and age (today, past week, past month), as chips rather than a menu.
- Search across every class, matching name, class, folder path and type, word by
  word. The results strip says how many matched and carries the way out; there is
  also an ✕ in the box, and Escape clears it.
- Office previews: Word, PowerPoint and Excel convert to PDF in the background on
  upload and render in the preview pane. The pane says "Converting…" while it runs,
  falls back to Download if conversion fails, and a file's converted copy is deleted
  with it. Old uploads are converted once at boot.
- Files show their folder in the list view, in search results, in ⌘K and in the
  preview pane, which is the last of Saif's metadata list.
- Notes embed files from any class, not just their own, and an embedded file opens in
  Vesta's preview rather than a new browser tab. Embedding also records a real
  `note_links` row on the local backend, which it previously only did in the cloud
  one, so the file shows under "What this note is about".

### Deferred, on purpose

- [ ] **Full content search.** Kept explicitly at Saif's request, 2026-09-14. A
      server-side endpoint searching `materials.extracted_text` alongside name, class,
      folder, type and date, so a file can be found by a phrase inside the PDF rather
      than only by its name. Two things to do first: upload one real PDF and confirm
      extraction actually produces text (see the corrected bug entry above), and decide
      whether it needs an index, since a `LIKE` scan over `extracted_text` is fine at
      today's volume and not at five semesters of readings.

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

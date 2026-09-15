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
- [ ] **3. Real push.** Google watch channels to `/api/calendar/google/webhook`, plus a
      Railway cron to renew them: channels expire and Google never renews them. Only
      worth doing once 1 and 2 are solid.

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
- [ ] **File folders.** Notes got a real folder tree (`note_folders.parent_id`); files
      got a single flat `category` string guessed from the filename. Fine at today's
      volume, inconsistent as it grows.

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

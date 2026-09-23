# Importing from Canvas

Decided 2026-09-21. Client built, the rest is not.

## The route: a personal access token, server-side. No browser extension.

Saif asked whether a Chrome/Firefox extension could pull Canvas files into Vesta's
classes. It could, but it is the worse of the two designs and we are not doing it.

SFU Canvas exposes the **+ New Access Token** button under Account -> Settings ->
Approved Integrations, which Saif confirmed. That is the whole question answered: with a
token, Vesta talks to `canvas.sfu.ca/api/v1` from Flask and needs no extension at all.
Server-side beats an extension on every axis that matters here:

- it works on his phone, where an extension cannot run;
- it can sync on a schedule instead of only when a tab is open;
- there is one codebase, not one plus a Chrome build plus a Firefox build plus store
  signing for both;
- an extension would have needed backend work anyway. The session cookie is
  `SameSite=Lax` (`auth.py`), so an extension's cross-site fetch to vesta.study would
  not send it, meaning a bearer-token API path or a content script injected into an
  open Vesta tab. "Just an extension, no server changes" was never available.

The extension only wins if a university disables token generation, because it can ride
an existing Canvas browser session. SFU does not, so that advantage is worth nothing.

## What the Canvas API gives us

Verified against the Canvas LMS API docs, not from memory. All calls take
`Authorization: Bearer <token>` and paginate via the `Link` header with `per_page`.

| Need | Call |
| --- | --- |
| His courses | `GET /courses?enrollment_state=active&include[]=term` |
| Whether weights are even in use | `apply_assignment_group_weights` on the Course object |
| Assignments, weights and his scores, in one pass | `GET /courses/:id/assignment_groups?include[]=assignments&include[]=submission` |
| Assignment detail | `due_at`, `points_possible`, `description` (HTML), `submission_types`, `html_url`, `published`, `omit_from_final_grade`, `rubric` |
| Group weight | `group_weight` on the AssignmentGroup, a percent of the final grade |
| Files | `GET /courses/:id/files`, `GET /courses/:id/folders` |
| File detail | `display_name`, `size`, `content-type`, `folder_id`, `modified_at`, `url` |
| Files when the Files tab is off | `GET /courses/:id/modules?include[]=items`, then `/files/:id` per file item |

Two things to know about files. The file `url` is a `/files/:id/download` endpoint that
wants the token, so a stored bare link is only openable in a browser already signed
into Canvas; server-side downloads must send the header.

And **a course can hide its Files tab**, in which case `/files` answers 403. Saif asked
for the modules fallback explicitly, and it is built: `Client.course_files` tries
`/files` first, and on a 403 walks `/modules?include[]=items`, takes every item of type
`File`, and reads each one's file object. Module items also carry the module name, which
is better foldering than the flat Canvas folder tree anyway, so the fallback is in some
courses the nicer result. A 403 is never surfaced as an error.

## Scope, as decided

**Pull everything: files, assignments, group weights, and his actual scores.**

**Canvas wins unless he changed a grade by hand.** His words, 2026-09-21. See the score
rule below, which turned out to be a feature this app already has.

**Files are links first, bytes on demand.** A synced Canvas file is inserted as a
`materials` row with `kind='link'`, its `url` set to the Canvas download URL and
`stored_name` NULL. The first time he opens one, Vesta downloads it with the token,
writes it to the volume, sets `stored_name`, extracts its text and queues the Office
preview, exactly as an upload does. The existing schema already carries this: a row
with a `url` and no `stored_name` is the "not fetched yet" state, so nothing new is
needed to express it.

That keeps the Railway volume small (parked object storage stays parked, see `NEXT.md`)
and stops a five-course semester of lecture decks filling it on day one. The cost is
that file search, rubric parsing and Headstart context do not see a file until it has
been opened once. A "fetch everything in this class" button would be a small addition
if that turns out to be annoying.

The 25 MB `MAX_CONTENT_LENGTH` cap does not apply to a server-side download, since
nothing is posted through Flask. `Client.download` applies its own ceiling anyway and
streams to disk rather than holding a file in memory; over the ceiling the row stays a
link with a visible reason.

## Design

### Almost none of this needs new machinery

The first plan here added `canvas_id` columns to three tables and split each Canvas
group weight across its assignments by points. Reading the syllabus importer first
would have saved that: **both problems are already solved in this codebase**, and
copying its approach is smaller, better and consistent with what Saif already uses.

**Idempotent re-sync: `items.import_key`.** The column exists, the syllabus importer
matches on it (`syllabus_import.py`), and `app.py` already returns it as `importKey`. A
Canvas assignment gets `import_key = "canvas:<assignment id>"` and re-syncs match on
it exactly. **No schema change at all** for assignments.

**Weights: `grade_categories`, not per-item arithmetic.** Vesta has weighted categories
with `drop_lowest`, and it already shares a category's weight across the items in it.
That is exactly what a Canvas assignment group is. So a group becomes a
`grade_categories` row carrying its `group_weight`, its assignments get that
`category_id`, and no weight is ever split by hand. This also deletes the riskiest part
of the old plan, which was two different weight formulas needing a test each.

Points-based courses still need care: when `apply_assignment_group_weights` is false,
the group weights are meaningless, so import **no** categories and set each item's
`weight` to its `points_possible` share of the course total. One branch, not two
formulas.

**Scores, and the conflict rule.** Canvas `score` is points earned; Vesta's
`items.score` is a percent, because the grade maths is
`weight * score / 100` (`static/index.html`). So a score maps as
`score / points_possible * 100`, and an assignment with no `points_possible` cannot
carry one.

Saif asked for "Canvas wins unless I change the grade by hand", and the syllabus
importer's review flow is already that policy: it marks each field `new`, `changed` or
`same`, fills a gap without asking, and shows a differing value with its tick **off**
so nothing overwrites what is already there without a look. Canvas sync should produce
the same draft shape and reuse that screen rather than invent a second conflict
mechanism. An empty score is filled silently; a score that disagrees with Canvas is
shown, unticked, with both numbers.

Only a submission with `workflow_state == "graded"`, no `excused`, and a non-null
`score` counts. A submitted-but-ungraded assignment has a `score` of null and must not
land as a zero.

### The rest

**The token** lives in `app_settings` under its own key, `canvas`, via
`db.get_setting` / `db.set_setting`. That table is already keyed per user and already
covered by row level security, so this needs no schema change and no new policy, for
the same reason `prefs.py` gave. It must **not** go into the `prefs` JSON blob, which
is served to the browser in full. The settings endpoint returns whether a token is
present and which Canvas host it is for, never the token itself.

**Course to class mapping is shown, not guessed silently.** Sync opens a screen listing
his Canvas courses beside his Vesta classes, obvious matches by course code
pre-selected, everything else an explicit choice including "create a new class" and
"ignore this course". The mapping is saved so later syncs are one click. A wrong silent
match would file a term of materials under the wrong class.

The only schema addition left is somewhere to keep that mapping and the last sync time.
A `canvas` JSON blob in `app_settings` can hold `{course_id: class_id}` and needs no
migration, which is the better trade while this is one user's feature. If it later
wants to be a column, remember the `NEXT.md` rule: every SQLite ALTER needs its partner
block in `cloud/migrate/pg_migrations.sql` **in the same commit**, or the feature
reaches production with none of its schema. That has bitten twice.

## Build order

1. **`canvas.py`: the API client. Built.** Token handling, `Link` pagination, the
   403-to-modules fallback, streaming downloads with a ceiling, and the pure mapping
   layer (due dates, types, weights, scores, course matching). Tests in
   `tests/test_canvas.py`, no network and no token needed.
2. The sync engine: build a draft in the syllabus importer's shape, match on
   `import_key`, create categories, then apply only what is ticked.
3. Lazy file fetch on first open.
4. UI: the token field in Settings, the mapping screen, a Sync button per class and a
   sync-all on the Classes screen.

Step 4 is the only part that touches `static/index.html`. Steps 1 to 3 do not, so the
backend can be built while something else is editing that file.

## Open questions

- Should sync run on a schedule, or only when he presses the button? A schedule means
  going through `for_each_account` (`NEXT.md`), and means holding a token that can
  expire without anyone noticing until a sync fails.
- Announcements and Canvas calendar events are available on the same token and are not
  in scope. Say no for now.
- Canvas `rubric` comes free on an assignment and could feed the rubric-criteria path
  that today needs a parsed PDF and an AI call. Worth doing, after the basics.

## Found while building the client

**This machine's timezone database has BC leaving daylight saving for good on
2026-11-01**, after which `America/Vancouver` is a permanent -07:00 and reports itself
as MST. Whether that lands is a political question, but it means two copies of tzdata
can disagree about every due time after 1 November by an hour, so no test here asserts
a raw offset; `test_the_conversion_uses_the_same_zone_as_the_rest_of_the_app` pins the
behaviour to whatever `ics.zone()` says instead.

The related hazard is worse and is not about Canvas. `ics.zone()` returns None when
`ZoneInfo` cannot find its data, and every caller then quietly works in UTC, which
turns an 11:59pm deadline into the following day across the calendar, the .ics export
and Google sync alike. A slim container has no `/usr/share/zoneinfo` and nothing in the
app would report it. **`tzdata` is now in `requirements.txt`** for that reason: pure
data, no code, and it makes the fallback unreachable. Worth checking a deployed due
time once after the next deploy.

## What his actual courses turned out to look like

Run against his real SFU account on 2026-09-21, Fall 2026: **5 courses, 80 assignments,
88 files, 766 MB.** Four findings changed the code, and none of them were guessable
from the API docs.

**Four of the five courses have the Files tab switched off.** IAT201 is the only one
with it on. The modules fallback Saif asked for is not an edge case, it is the main
path, and without it this feature would reach 2 files instead of 88. Module names also
turn out to be good folder names: "Week 1: Introduction", "Welcome Package",
"Course Assignments".

**766 MB confirms links-first was the right call.** IAT201 alone is 617 MB. Downloading
everything on sync would have put most of a gigabyte on the Railway volume for one
student's one term.

**Two of the five courses are points-based, and their weights are worthless.** IAT201
has 25 assignments all worth zero points, so every derived weight is 0%. PSYC300W has
five assignments set up out of a term's worth, which makes "Writing Skill 1" 62.5% of
his grade. Both numbers follow correctly from what Canvas holds and both are wrong
about the course. `plan_course` now returns `weightsFromCanvas`, and the review screen
must leave every weight tick off when it is False. For those courses the syllabus
importer stays the source of truth for weights; Canvas is still the better source of
files, titles, types and due dates.

**Canvas groups are used as per-assignment weights, and as scratch space.** REM388 had
eleven groups, seven holding a single assignment; SD381 had eight, including "Imported
Assignments" and "Unused assignments for this offering", both empty and both at 0%.
So `weighting` now drops a group with nothing in it that counts, and collapses a group
holding one assignment into that assignment's own weight. REM388 comes out with three
categories instead of eleven and SD381 with two instead of eight, with every percentage
preserved. This is cosmetic rather than behavioural, because Vesta already shares a
category's weight across its items, so a one-item category and a plain weight compute
identically. The one wrinkle: if an instructor later adds a second assignment to one of
those groups, the next sync will want to turn it back into a category.

**Type guessing needed one word.** A whole course names its readings "Read Noba
Attention & Failures of Awareness", which matched nothing and came through as plain
assignments. "read" is in the vocabulary now, placed after "quiz" and "exam" so a
"reading quiz" stays a quiz. His real titles are pinned as test cases.

Two more things worth knowing before the sync engine is written:

- **Scores are mostly absent this early in the term.** Four graded items across five
  courses. The conflict rule matters much less right now than it will in November,
  which is an argument for shipping the import before the conflict screen is perfect.
- **IAT201 has almost no due dates in Canvas.** Its 25 assignments carry none at all,
  so for that course the syllabus remains the only place the schedule exists. A sync
  must never interpret "Canvas has no due date" as "clear the due date I already have".

## What happens when a sync finds something Vesta already has

Saif raised this on 2026-09-22, and it found two holes in the plan above.

Every planned assignment and file comes back from `canvas_sync.compare` with one of
three verdicts, and the review screen shows them as a tick per change:

* **new** — Vesta has nothing like it. Ticked on.
* **same** — matched, and nothing differs. Nothing to do, not shown as an action.
* **changed** — matched, but a field disagrees. Both values are shown and the tick
  starts **off**. A field Vesta has not got at all is a fill, and those are ticked on,
  because there is nothing to overwrite. This is what "Canvas wins unless I change the
  grades by hand" comes to once you notice a column cannot tell a typed value from a
  synced one: the tick is the hand.

Ticks are per field, not per assignment, so a moved deadline can be accepted while a
weight he set himself is kept.

**Nothing is ever deleted.** An assignment or file Vesta has and Canvas does not is not
offered for removal, unlike a full syllabus re-import, because Canvas is one source
among several in a class: the syllabus importer and his own typing are the others.

### The first hole: an assignment he already typed

Matching on `import_key` alone only recognises rows a previous Canvas sync wrote. A
class whose assignments came from a syllabus import carries no Canvas keys, so the
first sync would have duplicated every one of them. `_match_item` now falls back to a
normalised title, exactly as `syllabus_import.compare` does, and a test asserts the two
importers normalise titles identically so they always agree about when two assignments
are the same one.

### The second hole: files had no matching at all

The plan above never said how a Canvas file relates to a file already in the class, and
he has been uploading these by hand all term. Two rules now:

* a Canvas file is matched by its key, then by **filename and byte count**. Same name,
  same size, same file: left alone, not duplicated.
* same name, **different size**, and it is a conflict shown with both sizes and the
  tick off. The option to *replace* is deliberately not offered. The copy in Vesta may
  be the one he annotated and it exists nowhere else, so the choice is keep what is
  here, or take both. Replacing can be added later as an explicit action on one file;
  it must never be something a sync does on its own.

### Correction to the schema claim above

The "no schema change at all" line is right for assignments and wrong for files.
`materials` has no `import_key`, so recognising a file this feature wrote needs one
column, mirroring `items.import_key` exactly. The alternative considered and rejected
was parsing the Canvas file id back out of `materials.url`, which works until someone
pastes a Canvas link into a class themselves and the sync decides it owns that row.

One `ALTER TABLE materials ADD COLUMN import_key TEXT`, and **its partner block in
`cloud/migrate/pg_migrations.sql` in the same commit**. `canvas_sync._col` already
tolerates a row read before that migration has run: the filename match carries it.

## Decisions of 2026-09-22

**Prefetch: files at or under 5 MB, never video.** Measured against his own library
first: 70% of his 88 files are under 5 MB but only 9% of the bytes, so this fetches
most of his material for 68 MB instead of 766. 283 MB of the rest is mp4, which yields
no text to search or to give Headstart, so video is never fetched at any size. A file
whose size Canvas did not report is fetched, because guessing the other way leaves a
syllabus unsearchable.

**Triggered by buttons and by a daily background sync.**

**A deleted assignment stays deleted.** Each course's entry in the stored state keeps
`seen`, the Canvas keys previous syncs wrote. A key that was written once and now
matches no row means he deleted it on purpose, so it comes back marked `dismissed`,
stays off the review screen, and is counted so the screen can offer "3 you deleted are
hidden, show them". `forget()` is that button. This needs no column and no hook on the
delete route, and it is self-correcting.

## Built on 2026-09-22

- `materials.import_key`, in `db.py` **and** as migration `010_materials_import_key` in
  `cloud/migrate/pg_migrations.sql`, in the same commit, per the `NEXT.md` rule.
- `canvas_sync.apply_draft`, which writes only what was ticked. An assignment that
  already exists never has its title, status, subtasks or untouched fields rewritten by
  a sync; it does always gain its Canvas key, so the next sync matches exactly rather
  than depending on the title still agreeing.
- The stored state in `app_settings` under `canvas`: token, host, per-course mapping,
  `seen`, last sync and last error. `public_state` is what the browser sees and it
  never contains the token.
- A Canvas module becomes a `kind='custom'` folder, so it can be renamed or deleted
  like one he made himself.
- 222 tests pass.

## Still to build, and the problems each one has (superseded: see "Where it stands" at the end)

### The endpoints

Connect and disconnect, list courses with the suggested mapping, sync one course, apply
a reviewed draft, and fetch a file's bytes on first open. The archived-term guard needs
no work here: `_guard_archived_semester` is a `before_request` on every write to
`/api/`, so a sync into an archived term is already refused with 423.

### Fetching a file's bytes on first open

The row is a link with `stored_name` null. The first open downloads it with the token,
writes it to the volume, sets `stored_name`, extracts its text and queues the Office
preview, exactly as an upload does. Two things to get right:

- **two clicks on the same unfetched file.** Download to a temporary name and rename
  into place, so the worst case is the same bytes written twice rather than a file
  half-read while it is half-written.
- **the wait.** A 20 MB deck takes seconds, and the request holds a thread. Prefetching
  at 5 MB means most opens are instant, but the big ones need the interface to say it
  is fetching rather than appearing to hang.

### The daily background sync

This is the part with real failure modes, and all of them come from the same place:
**nobody is watching when it runs.**

- **It must go through `for_each_account`.** Anything running outside a request has no
  `g.user_id`, and `NEXT.md` is explicit about this.
- **It must check the term itself.** The archived guard is a `before_request` and a
  background job never passes through one.
- **A dead token fails silently.** A token can expire or be revoked in Canvas, and the
  next 40 syncs then fail where nobody is looking. `lastError` is stored per account
  and per course for exactly this, and it has to be **visible in the interface**, not
  only in a log: Settings should say the connection is broken, and the Classes screen
  should show it too, because that is where he will be when it matters.
- **What may a background sync apply, given it cannot ask?** It applies exactly what
  the review screen would have arrived pre-ticked: new assignments, new files, and
  fills into fields Vesta has nothing in. Anything that disagrees with something
  already there is left unapplied and counted, so the class shows "4 changes to
  review". That is his rule, "Canvas wins unless I changed it by hand", carried out
  by a process that cannot see his hand: it only ever fills blanks.
- **Two syncs at once.** The daily job and the button can overlap. A sync in progress
  should be recorded in the state so the second one waits rather than both writing.
- **Cost of the first run.** About 55 Canvas calls and 15 to 20 seconds for five
  courses, because a course with its Files tab off needs one call per file. Later runs
  are about 4 seconds: a file already carrying its key is not re-read. The consequence
  accepted is that a file **replaced** on Canvas in a course with the Files tab off
  will not be noticed, because noticing means re-reading all 35 file objects. A "check
  for updated files" action can do the slow pass when he wants it.

## Decisions of 2026-09-22, second round

**Nothing Canvas finds goes into Vesta until he has seen it.** Not even a fill into an
empty field. The earlier plan had the background sync apply the safe parts on its
own; Saif chose instead that it only *finds*, and that he reviews everything, as
"x changes to review", before any of it is written.

- **How he is asked:** a banner across the top that names what happened ("PHIL110
  midterm moved Oct 19 to Oct 21, 2 new grades, 5 new files"), plus a strip on each
  affected class page. Nothing blocks the page.
- **When Canvas is checked:** when he opens Vesta and the last check is more than
  three hours old, and once a day as a backup.
- **Bulk:** changes grouped by kind, moved deadlines and grades first, each group with
  its own Accept all and every row still visible underneath.
- **Check for updates:** a button that re-reads every file, which is the only way to
  notice a file a professor replaced in place in a course with its Files tab off.

## How it works now

**Checking and reviewing are separate, and that split is the design.** A check talks
to Canvas and stores what it found as a snapshot per course (`app_settings`, key
`canvas_snapshot:<course id>`). It never writes a class row. A review holds the
snapshot up against his classes as they are at that moment, computed fresh on every
request, so there is no stored review to go stale: an edit he made a minute ago is
already reflected.

**Every change has an id built from what it would do**: the course, the assignment or
file, the field, and both values. Applying re-finds each accepted id in a freshly
computed review. If he edited the assignment in another tab after looking, or a newer
check landed with a different value, the id no longer exists, and that change is
skipped and reported instead of written over something he did not see. Tested both
ways.

**"Keep mine" is remembered against the value Canvas offered**, not the assignment. He
keeps his 88 over Canvas's 91 and is not asked again; Canvas re-marks it to 94 and that
is news, so it is offered. Refusing a new assignment or file is treated like deleting
one, and "show hidden" brings any of them back.

**"Later" is remembered against what was showing**, not a time. The banner, or one
class's strip, stays hidden until a check finds something that was not there when he
said later. Stored server-side, so later on the laptop is later on the phone.

**A moved deadline is one change**, even when the date and the time both moved. A date
Canvas fills into an empty field is its own group ("Due dates Canvas can fill in")
rather than being called a move.

**Things a review never does**, each because of a real case:

- move an existing assignment into Canvas's grade category when a different change to
  it is accepted. That would be a second change he was never shown.
- change the weight of a grade category he already has, unless that exact change was
  accepted.
- offer weights from a points-based course at all. The first version offered them
  unticked; against his real courses that left 32 junk weight changes (IAT201's 0%,
  PSYC300W's) on every review, permanently.
- replace a file he uploaded. A same-name file is offered as "Add Canvas's copy too"
  or "Keep mine". Only a file Canvas itself put there can be *updated*, when the
  professor replaced it.

**Fetching a file's bytes**, on first open or by prefetch, downloads to a temporary
name, renames into place, and only then switches the row from a link to a file. A
replaced file's old copy is deleted only after the new one is on disk. One lock per
file, so two clicks download once. A file over the 100 MB ceiling (his largest is 113
MB) stays a link and the page offers to open it on Canvas.

**The background check** goes through `for_each_account`, checks the archived-term
rule for itself, takes a per-account lock so the button and the timer cannot overlap,
re-reads the stored state before saving so a snooze or a mapping change made while
Canvas was answering is not overwritten, and records every failure in `lastError`
where the interface can show it.

## Run against his real account, 2026-09-22

Into a throwaway database, all five courses mapped:

- first check **24 to 28 seconds**, no errors. Slower than the estimate of 15 to 20.
- review: **171 changes**, 82 new assignments and 89 new files.
- accept all: 82 assignments, 89 files, 7 grade categories, 20 folders from module
  names; **63 files (69 MB) queued to fetch** under the 5 MB rule.
- review straight afterwards: **0**. A second check: **11 seconds**, then **0**. The
  second check is slower than the estimated 4 seconds because each course still costs
  three requests at roughly 0.7 seconds each at SFU. It runs in the background, so
  this only matters for how soon the banner appears after opening Vesta.
- a real file downloaded from each course came back as genuine bytes (a JPEG from
  IAT201's Files tab, PDFs from the four modules-only courses), not a sign-in page.

## Still to build: the interface (built 2026-09-22, see below)

The only part that touches `static/index.html`. Everything it needs exists:

| Route | For |
| --- | --- |
| `GET /api/canvas` | connection state, `stale`, `checking`, and the banner summary |
| `POST /api/canvas/connect` | verify and store a token, return courses with suggested pairings |
| `DELETE /api/canvas` | disconnect, keeping pairings and refusals |
| `GET /api/canvas/courses` | courses and suggested pairings again |
| `PUT /api/canvas/mapping` | pair courses with classes, `"new"` makes the class |
| `POST /api/canvas/check` | `{ifStale}` on open, `{full}` for Check for updates |
| `GET /api/canvas/review` | the full review, grouped, optionally `?classId=` |
| `POST /api/canvas/apply` | `{accept: [ids], reject: [ids]}` |
| `POST /api/canvas/snooze` | Later, for the banner or one class |
| `POST /api/canvas/unhide` | show what he deleted or refused |
| `PUT /api/canvas/auto` | the automatic-check switch |
| `POST /api/materials/<id>/fetch` | open a Canvas file that is still a link |

`serialize_material` now carries `fromCanvas`, and `notFetched` with the file's name
and size while it is still a link, so the page can fetch instead of opening Canvas.

Standing preferences that shape it: controls and anything needing his attention shown
directly, never behind a chevron or a bare count; styled dialogs, never native ones;
the sidebar is navigation only; Settings holds only what cannot be set anywhere else;
nothing gated behind a setup prompt; friends will use this, so it has to make sense to
someone who has never seen it.

## The interface, built 2026-09-22

All in `static/index.html`, in one block under `// ---------------- Canvas ----------------`,
reusing the page's own pieces: the syllabus review's full-screen panel and footer
(`si-panel`, `si-foot`), Settings cards (`st-card`, `set-row`, `set-switch`), the styled
dialogs, and the colour tokens, so it follows light and dark themes without its own.

- **Banner**, in `#canvas-banner` above every page: names what happened ("PHIL 110
  In-lecture midterm exam 1 moved Oct 17 12:20pm to Oct 19 12:20pm, 1 grade that
  differs from yours"), with Later and Review. A failed check turns it red and stays
  up until a check succeeds, whatever else is going on.
- **Class strip** at the top of an affected class's page, the same for that class
  alone, and a **Canvas button** in the class header on every paired class, with the
  count beside it, so Check for updates is never out of reach.
- **Review**: groups in order of consequence, Accept all and Keep all per group, a
  two-way choice on every row, nothing applied until Apply. What was done is said at
  the top afterwards, including anything skipped because it moved since he looked.
  Hidden things per class, with Show them again.
- **Settings > Integrations > Canvas**: address, token (never shown back), plain steps
  for making a token, pairings with suggestions filled in, the Check on its own switch,
  Use a new token, Disconnect. Nothing else, per the rule that Settings holds only what
  cannot be set anywhere else.
- **Opening a Canvas file that is still a link** fetches it with a progress state and
  then opens it as a file; if Canvas will not send it, says why and offers Open on
  Canvas. One hook in `openFilePreviewModal`, so it works from every list.
- **Checking on open**: `canvasBoot` runs once after the first state load, and on
  returning to the tab at most once a minute; it checks only if the last check is
  over three hours old and the switch is on.

A class made from a course is named the way a student writes it ("SD 381", "PSYC
300W", not "SD381 OL01"), without the code Canvas repeats in the name, and takes the
first palette colour no class in the term has.

**A pre-existing bug fixed on the way**: some file names in a class's Files tab are
drawn as `<a href="#">`, and the click handler never cancelled the link, so opening any
file from there sent the page behind the preview to the Dashboard. It predates this
work (it is in `7bd6381`). One line, `e.preventDefault()` in the `preview-file` action.

### Verified in a real browser against his real Canvas

Playwright against a throwaway local copy (`DATA_DIR` in /tmp), token read from `.env`:
a wrong token shows Canvas's refusal; the real one connects and suggests IAT201 B100
for IAT 201 and PHIL110 D100 for PHIL 110; a check of three courses takes about nine
seconds and brings up the banner (85 changes); the class strip, phone width and dark
theme all render; Accept all plus one skip applies 43 assignments, 41 files and 4
categories and starts 26 downloads; the banner then disappears; a Canvas PDF opens as a
file with its text extracted (Vesta's related-assignment suggestions found it). A moved
deadline and a disagreeing grade, simulated by editing the database, show in their own
groups; accepting the move and keeping the grade does exactly that. No page errors.

## What is left

- **Committing.** `static/index.html` and `NEXT.md` also hold another session's
  uncommitted chat-naming change. It is intact and can be separated: a copy of that
  session's diff was saved before this work started, and it still reverse-applies
  cleanly, so the Canvas work can be staged without it.
- **Deploying.** On push, Railway runs migration `010_materials_import_key` on its own.
  Per `NEXT.md`, check `/health` for `migrations` afterwards. The token is then pasted
  into Settings on vesta.study; the `CANVAS_TOKEN` line in `.env` was only for testing
  scripts, and the app never reads it.
- **More than one gunicorn worker** would run one daily loop per worker. Harmless today
  (one worker, see `Procfile`), and at worst one redundant check, but worth knowing.
- **Parked, not built:** Canvas rubrics into the rubric-criteria path; announcements
  and Canvas calendar events; moving an assignment he already had into Canvas's grade
  category (deliberately never done as a side effect of another change).

# This Week: a checklist of what to do, built from Canvas

Asked for by Saif on 2026-09-24: *"something that gives me a checklist of what I have
to do for the week ... I want to know what I should study/work on for the week."* His
model is IAT 201's Course Progression Map, a document split week by week with dated
readings, "START" cues about ten days before anything big is due, and the line
"Tick items off as you go."

## Decisions of 2026-09-24

All four were Saif's picks, each the recommended option.

- **Its own page**, "This Week", with arrows to step between weeks, grouped by class.
  The Dashboard gets one line pointing at it.
- **Work and readings are checkboxes; slides, handouts and resources are links.**
  Readings, lectures to watch, discussions, quizzes and assignments can be ticked.
  Lecture slides, handouts, tutorial PDFs and resource pages are listed under the week
  as links. Any row can be switched between the two, and the switch is remembered.
- **Documents are read by AI into the checklist, reviewed first.** For a course whose
  plan lives in a document (IAT 201's map, PHIL 110's syllabus). One AI read per
  document, and nothing lands until he approves it, like the Canvas review.
- **START cues for every course, not just IAT 201.** Exams and heavily weighted
  assignments get a "start studying" or "start drafting" item about ten days before
  they are due.

## What his courses look like (surveyed 2026-09-24)

| Course | Where the week lives | Notes |
| --- | --- | --- |
| REM 388 | A module per week, "Week 01" to "Week 14", all posted | Each week: discussion, quiz, two handouts, a tutorial PDF. "Week 07 - Midterm - Monday, 19 Oct". |
| PSYC 300W | A module per week, "Week 2: Our Changing Relationship..." | Sections as SubHeaders: READINGS, ASSIGNMENTS, LECTURE SLIDES, TUTORIAL ASSIGNMENTS. Posted about a week ahead. **Its week numbers run one behind the calendar** (its Week 3 is the week of Sep 29). |
| SD 381 | A module per week, "MODULE 1, WEEK 2" | Online. Released weekly. The only course using Canvas completion requirements (`must_view`), which report `completed`. |
| IAT 201 | Themed modules with "Week N" SubHeaders inside | No Canvas due dates at all. The Course Progression Map (a .docx in "Course Overview") has every date, reading and START cue. Canvas modules and the map disagree in places (Canvas puts "Attention" in week 4, the map in week 5). |
| PHIL 110 | Nowhere in Canvas | Modules hold Crowdmark and slide decks named "Week 1 - ...". The plan is in the syllabus. |

## How it works

**Nothing new is fetched except modules, and nothing about his classes is written.** A
Canvas check already stores a snapshot per course. It now also stores the course's
modules, reduced to the fields this page needs. The page computes the weeks from the
snapshot fresh on every request, the same way the Canvas review does, so there is no
stored plan to go stale.

**Splitting modules into weeks** (`week.py`):

- A module whose name says "Week N" is week N ("Week 01", "Week 2: ...",
  "MODULE 1, WEEK 3").
- Otherwise a SubHeader saying "Week N" starts week N inside the module, until the next
  week SubHeader (IAT 201). Any other SubHeader is a section label within the week
  ("READINGS", "LECTURE SLIDES", "Working Section").
- A module with no week in it ("Course Overview", "Writing and Reading Resources") is
  not part of any week.
- An item whose own title starts "Week N" in a module that is not a week (PHIL 110's
  "Slides" module) goes to week N as a link.

**Turning a week number into dates.** Week 1 is the Monday-to-Sunday week containing
the term's first day (`syllabus.resolve_week`'s convention; Fall 2026 starts Wed 9
Sep). Courses do not agree on this, so each course's offset is worked out from dates
written in its own module names and SubHeaders: "do before Sep. 22 class" in PSYC
300W's Week 2 falls in calendar week 3, so that course is shifted by one. REM 388's
"Week 07 - Midterm - Monday, 19 Oct" confirms zero for it. The most common offset
wins; with no written dates it is zero. A per-class shift can be set by hand when the
guess is wrong.

**Checkbox or link**, decided by the item's Canvas type, its section label and its title:

- Assignment, Quiz, Discussion: a checkbox. If Vesta already has that assignment
  (matched by Canvas id, or by title within the class), ticking it marks the
  assignment done and the assignment's own done state shows here.
- File, Page, ExternalUrl: a checkbox when it reads as work (under a READINGS section,
  a title starting "Read", a lecture video, a page in an online course's week); a link
  when it reads as material (slides, handout, tutorial PDF, "LECTURE SLIDES" section).
- Canvas's own completion (`completion_requirement.completed`) pre-ticks an item.

**What else shows in a week:**

- Assignments in Vesta due that week that no module mentions, so nothing due is
  missing because a professor did not put it in a module.
- START cues, computed from assignments: an exam, a midterm, a final, or anything
  worth 10% or more gets "Start studying for ..." or "Start ..." in the week ten days
  before its due date. Ticking one is remembered against the assignment.

**Tick state** lives in `week_marks`: one row per ticked or overridden row, keyed by a
string built from where it came from (`canvas:<course id>:mi:<module item id>`,
`cue:<item id>`), so a re-check of Canvas never loses a tick. Assignment ticks are the
assignment's own status and are not duplicated there.

## Build order

1. **Modules into the snapshot, and `week.py`**: splitting, week offsets, classifying.
   Tested against his real module shapes, pinned as fixtures.
2. **`week_marks` table and `/api/week`**, with the SQLite migration and its Postgres
   partner in `pg_migrations.sql` on the same day.
3. **START cues and assignments due that week.**
4. **The This Week page**, and the Dashboard line.
5. **Documents read by AI**: IAT 201's map and PHIL 110's syllabus, into dated weekly
   items, through a review screen. Stored in their own table because, unlike module
   items, they are not in any snapshot.

## Decided 2026-09-24, second round

- **A class with an approved document plan takes its to-dos from the document.** IAT
  201's Canvas modules and its map disagree about which week some readings are in, and
  the map is the one the TA says to follow. Canvas module items that match nothing in
  the plan still show, as links, so nothing on Canvas is hidden. Saif: "I think that
  should be good."

## Built on 2026-09-24 (steps 1 to 4)

On branch `this-week`, in the worktree `~/Developer/vesta-week`, kept apart from the
notes-editor work going on in the main checkout at the same time. Not merged, not pushed.

- `week.py`: modules into weeks, course offsets, checkbox-or-link, START cues, the week
  assembled per class, and three routes: `GET /api/week?start=&today=`,
  `POST /api/week/marks`, `POST /api/week/shift`.
- `canvas.py`: modules are read with `content_details` (due dates), and `course_files`
  reuses them instead of reading them twice. Plan items carry `quizId`.
- `canvas_sync.check_course` stores the reduced modules in each course's snapshot. A
  course with its Modules tab off is not a failed check.
- `week_marks` in `db.SCHEMA` and migration `011_week_marks`, in the export list.
- The This Week page and a This Week card on the Dashboard (`static/index.html`).
- Tests: `tests/test_week.py` (his real modules, `tests/fixtures/modules_fall2026.json`),
  `tests/test_week_assemble.py`, `tests/pg/test_week_pg.py` (two students in one course
  keep their own ticks).

**Found against his real account and fixed:**

- SD 381's "upcoming video assignment due Oct. 4th" was read as that week's date and
  moved the whole course two weeks late. Dates in a line saying due, upcoming or next
  no longer vote, and no vote more than one week out counts.
- "Overview" made IAT 201's "DDA1: Workflow Overview" a to-do. Dropped from the words
  that make a page work.
- Canvas does not say which assignment a module's discussion is (the topic id is not in
  `assignment_groups` without another include), and matching on the shortened title
  missed SD 381's "MODULE 1, Week 1, Part 3b". Matching now tries Canvas's full title
  first.
- The class cards overflowed a phone screen: the grid's 380px minimum was wider than
  the screen.

**Checked in a browser** against a local copy filled from his real Canvas (all five
courses checked, 82 assignments applied, files not applied): ticking, reload keeps the
tick, previous and next week, the Dashboard card, the week shift and its reset, Not a
to-do and back, opening an assignment, clearing earlier weeks, and a 390px phone with
no sideways scroll. No console errors.

## Still to build

- [x] **Step 5, documents read by AI.** Built the same day; see below.
- [ ] **needs Saif**: after merging, one Canvas check fills the modules in for every
      course; until then the page says so and offers the check.
- [ ] Carried-over assignments are only as right as their status. An assignment
      submitted on Canvas but not ticked in Vesta shows as still open from an earlier
      week, as it already does on the Assignments page.
- [ ] Merge `this-week` into `main` once the notes-editor work in the main checkout is
      committed. `static/index.html` is the file both touch.

## Built on 2026-09-24 (step 5): a plan from a document

- `week_plan.py`: which documents a class could be planned from, the paid read (cost
  shown and confirmed first, refused over the day's cap, recorded in `ai_usage` as
  `week_plan`), the draft held in `app_settings` until reviewed, and the routes
  `GET /api/week/plan/<class>`, `POST .../read`, `POST .../apply`,
  `DELETE /api/week/plan/<class>` and `DELETE .../draft`.
- `week_plan_items` in `db.SCHEMA` and migration `012_week_plan_items`, in the export.
- `week.assemble_class` takes the plan: its rows are the to-dos, its topics and notices
  are the week's, its own START cues replace Vesta's, and each Canvas module item is
  folded into the plan row it matches (lending its link and its assignment), or shown
  as a link. **Refinement of Saif's rule:** a Canvas item that is a graded assignment
  in Vesta stays a to-do even when the plan does not list it, because IAT 201's map
  says Muddy Points are due "each Wednesday" without listing them week by week.
- The page: "Plan from a document" on every class card (and on a class with nothing
  this week), a modal that goes pick, cost, read, review, add, and "Read again" and
  "Stop using it" once a class has a plan.
- `tests/test_week_plan.py`, with the model's reading of IAT 201's map pinned as
  `tests/fixtures/plan_iat201_draft.json`.

**Sources.** Files in the class, and files linked from the course's Canvas Syllabus page
that Vesta does not hold. PHIL 110's syllabus exists only there: its Files tab is off
and no module links it, so no Canvas sync ever brings it in.

**Model and settings.** The account's configured model (Sonnet 5 by default) at
`effort: "low"` with a 32,000-token ceiling. At the default effort IAT 201's map used
all of 16,000 tokens thinking and returned nothing. Measured: IAT 201's map, 7,100
tokens in, about $0.06, 30 seconds; PHIL 110's syllabus, 3,800 in, $0.02, 9 seconds.

**Found in testing and fixed:**

- PHIL 110's syllabus page links the syllabus and a 300-page textbook, and the textbook
  was the default pick. Only names that look like a schedule are suggested now, the
  strongest first. Reading the textbook cost $0.10 and found nothing; a read that finds
  no weekly plan now says so instead of opening an empty review.
- IAT 201's map names one reading twice (by author, as homework "for next week", then
  by title in its own week). The instructions now say to keep only the second.
- The estimate assumed a flat 8,000 tokens out and showed $0.09 for a $0.02 read. It
  now scales with the document.

**Checked in a browser** against the local copy: PHIL 110 from the Canvas Syllabus
page, IAT 201 from its map in Files, ticking and unticking in review, adding, the card
afterwards. About $0.51 of API spend across all testing.

## Automatic, 2026-09-24 (supersedes the review flow above)

Saif, on seeing "Plan from a document": *"I don't like the fact that it's a new button
with me doing the work ... why can't it just go through my course material on canvas
... and just autofill it accordingly."* He then chose, from two options each: scan
everything for free and pay to read only schedules (over reading every PDF, which would
have been $5 to $15 a term for little gain), and run it for every account, friends
included, on his key.

**How it works now** (`week_plan.py`):

1. After every Canvas check, inside the check's lock (so "checking" lasts until the
   week has what the check found), `auto_plan` runs for the account's classes, side
   by side.
2. For each class, candidates are found by name: module files and pages called
   syllabus, schedule, outline, progression, list of weeks and so on; every file the
   Canvas Syllabus page links; the Syllabus page itself; and the class's own files.
3. Each is scored for free by how many weeks of the term its text names. Measured on
   his: IAT map 13, PHIL syllabus 13, REM outline 14, SD 381 list 8, PSYC syllabus 24
   dates; PHIL's textbook 0. At least 5 weeks to count.
4. The best is read with AI only if its fingerprint (file id, size, modified time)
   changed since the last read. A file already scored is not even downloaded again.
5. What it says is added straight to the plan. Row ids are built from what a row says,
   so an updated schedule keeps the ticks on rows that did not change.

A class not on Canvas is planned the same way from its own files when This Week is
opened, since there is no Canvas check to hang it on.

**The schedule and the modules are merged, not ranked.** With every course planned
automatically, "the document wins and other Canvas items become links" would have hidden
real module work in PSYC 300W, REM 388 and SD 381. Now both show; a Canvas item the
schedule also lists (by title, within a week) is folded into the schedule's row, lending
its link, its assignment and any tick it already had.

**What is left for him to do:** nothing. Each card ends with one quiet line, "Filled in
from <document> · Change". Two buttons on every card was clutter for something rarely
used, so both corrections sit behind that one link (Saif agreed, before going live):
every candidate with how many weeks it lays out (choosing one pins it), and "Use Canvas
modules only". A class with nothing this week keeps the same link in the quiet list at
the bottom, which is where one switched to modules only usually ends up.

Switching to modules only hides the plan rather than deleting it, so switching back is
instant, keeps every tick, and does not pay to read the same document again.

**Measured on his real account:** the first check read all five schedules, correctly
chosen, for $0.25 in total (IAT $0.077, PHIL $0.021, PSYC $0.044, REM $0.048, SD 381
$0.061). One after another that took 185 seconds, longer than the page waits for a
check; side by side, a check with two reads took 68. A check with nothing changed made
no AI calls. About $0.84 of API spend across all of today's testing.

The review screen, the draft in `app_settings` and the pick-and-confirm modal are gone.

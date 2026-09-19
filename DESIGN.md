# Vesta — Design System

This is the source of truth for how Vesta looks. It was written after two passes: a first
redesign went editorial/broadsheet (serif headlines, hairline rules, zero radius) and missed —
Saif wants the soft, rounded, "bubbly" SaaS-dashboard look instead, based on reference
screenshots of the Snow content-engine app (`reference/*.png`). Any future visual change to
`static/index.html` should follow the rules below. If a change would contradict this file,
update this file first, then the code — don't let them drift apart.

## Reference

`vesta mockups/*.png` — **the current source of truth.** Nine full-page mockups of Vesta itself
(overview, classes, class detail, class notes, assignments x2, calendar, files, grades). Where
these disagree with anything older, these win.

`reference/*.png` — the earlier Snow screenshots that set the general direction: white rounded
cards with soft shadows, pill badges, clean geometric sans-serif throughout (no serif anywhere).
Superseded by the Vesta mockups on specifics like sidebar color and button color.

The light-mode `--bg`, card fill, and sidebar tokens below were corrected by pixel-sampling flat
regions of the reference screenshots directly (Python/Pillow, box-averaged to avoid
anti-aliasing) rather than eyeballed — the sidebar in particular came back much closer to true
black (`#090B0E` sampled) than an earlier eyeballed guess (`#121218`) had it. If the palette
needs re-checking against the reference again, sample flat fill regions (empty background,
inside a solid card, inside a solid pill) rather than text or thin strokes, which blend with
anti-aliasing and give unreliable readings.

## Foundations

### Color — light mode (default content area)

| Token | Value | Use |
|---|---|---|
| `--bg` | `#F4F5F7` | page/content background (cool light gray) |
| `--surface` | `#FFFFFF` | cards, modals, inputs |
| `--surface-2` | `#EDEFF3` | subtle fills — hover states, table header rows |
| `--surface-3` | `#E2E6EE` | stronger fill — dividers inside a card |
| `--border` | `#EBEEF4` | hairline card/input borders |
| `--text` | `#131722` | primary text |
| `--text-muted` | `#6B7280` | secondary text, meta |
| `--text-faint` | `#9CA3AF` | tertiary text, placeholders |
| `--accent` | `#005FFB` | links, icons, active nav/tabs |
| `--accent-ink` | `#0049C4` | accent text/border hover |
| `--accent-solid` | `#243145` | primary button fill (dark, not blue) |
| `--accent-solid-ink` | `#161F2E` | primary button hover |
| `--accent-soft` | `#E5EEFE` | tint fill behind accent pills/active nav |
| `--success` / `--success-soft` | `#1E9E5C` / `#DFF5E7` | done, active, positive |
| `--warn` / `--warn-soft` | `#C77700` / `#FCEED2` | due soon, paused |
| `--danger` / `--danger-soft` | `#DC2626` / `#FCE3E1` | overdue, disabled |
| `--danger-solid` | `#DC2626` | solid-fill danger button |
| `--none` | `#9CA3AF` | fallback when an item has no color |

### Color — dark mode

| Token | Value |
|---|---|
| `--bg` | `#17171D` |
| `--surface` | `#1E1F27` |
| `--surface-2` | `#24252F` |
| `--surface-3` | `#2B2D39` |
| `--border` | `#32333F` |
| `--text` | `#EDEDF2` |
| `--text-muted` | `#A3A3AF` |
| `--text-faint` | `#6E6F7C` |
| `--accent` | `#8FA6FF` |
| `--accent-ink` | `#AEC0FF` |
| `--accent-solid` | `#4C6FE0` |
| `--accent-solid-ink` | `#5C7EEA` |
| `--accent-soft` | `#232B45` |
| `--success` / `--success-soft` | `#3FCB7E` / `#1B3327` |
| `--warn` / `--warn-soft` | `#E4A93A` / `#3A2E14` |
| `--danger` / `--danger-soft` | `#F0685F` / `#3B211E` |
| `--danger-solid` | `#C4453B` |
| `--none` | `#75778A` |

**Why two accent tokens (`--accent` vs `--accent-solid`):** in dark mode a light accent color
reads fine as text/border on a dark background, but the same light value used as a *solid
button fill* washes out white button text. `--accent` is for text/links/borders/icons.
`--accent-solid` is only for backgrounds that carry white text (`.btn.primary`, `.lock-in-btn`
hover). Same split for danger. Don't collapse these back into one token.

### Sidebar — light, and theme-driven

**Grouping.** Pages are grouped by purpose, with a thin `.side-divider` rule
(`--sidebar-border`) between groups and no group labels: Dashboard and Calendar (where
you are); Classes, Assignments and Grades (coursework); Notes and Files (your material);
Headstart and Focus (study tools). Settings and the sync status sit in `.side-foot`,
separated by a full-width rule. The rules shrink with the collapsed rail.

The `vesta mockups/` set shows a **light** sidebar on every screen, so the sidebar now follows
the theme instead of staying dark. (An earlier pass had it permanently dark, from the Snow
references. The Vesta mockups supersede that.) Active nav item = soft blue fill, blue label,
blue icon.

| Token | Light | Dark |
|---|---|---|
| `--sidebar-bg` | `#F5F5F7` | `#14141A` |
| `--sidebar-border` | `#E7E8EC` | `#2A2B35` |
| `--sidebar-text` | `#131722` | `#EDEDF2` |
| `--sidebar-text-muted` | `#6B7280` | `#9A9BA8` |
| `--sidebar-hover` | `#ECEEF2` | `#20212A` |
| `--sidebar-active` | `#E7EDF4` | `#252938` |
| `--sidebar-accent` | `#005FFB` | `#7FA6FF` |

The sidebar holds nav plus Settings pinned at the bottom, nothing else. No stat readouts.

### Typography

One geometric sans-serif family for everything — headings and body. **No serif anywhere.**

- **Family:** Inter (400/500/600/700/800).
- **Headings:** bold (700), tight-ish tracking, sentence case (not uppercase, not italic).
- **Body/UI:** regular (400) or medium (500) at 14px.
- **Compact tabular data** — grade percentages, due dates/times, the Lock In countdown clock:
  IBM Plex Mono, via the existing `.mono` utility class. Big stat numbers (stat tiles, sidebar
  counts) use Inter bold instead, matching the reference. Mono is for small figures where digit
  alignment matters, not for headline numbers.
- **Micro-labels** (stat tile labels, sidebar section headers, table column headers, a group
  heading like "THIS WEEK"): uppercase, small (10.5–11px), letter-spacing ~.05em, `--text-faint`
  or `--text-muted`. This is a deliberate, reference-driven pattern here — unlike a generic
  eyebrow-above-a-headline, these labels are doing real information work (grouping a stat, a
  table column, a section of the sidebar), which is exactly how the reference uses them.

### Radius

| Token | Value | Use |
|---|---|---|
| `--r-card` | 16px | cards, modals, the hero/banner block |
| `--r-md` | 10px | buttons, inputs, stat tiles that aren't full cards |
| `--r-sm` | 8px | sidebar nav items, small icon badges |
| `--r-pill` | 999px | badges/status pills, segmented controls, filter chips |

Circles (avatar dots, color swatches, checkboxes) stay circular regardless.

### Shadow

Soft and barely-there — the border does most of the separation work, the shadow adds a little
lift:

```
--shadow-card: 0 1px 2px rgba(15,15,20,.04), 0 6px 16px -8px rgba(15,15,20,.10);
--shadow-pop:  0 20px 50px -12px rgba(15,15,20,.35);   /* modals only */
```

## Components

- **Sidebar nav item:** line icon + label, `--r-sm` radius. Active = `--sidebar-active` fill
  with `--sidebar-accent` (blue) label *and* icon. Hover (inactive) = `--sidebar-hover` fill.
  Icons are inline SVG from the `ICONS` map in the script, painted by `paintIcons()`; add new
  ones there rather than dropping in unicode glyphs.
- **Page header:** bold sentence-case h1, muted subtitle below, and on the right the current
  date plus the round avatar. This header is on *every* page, not just the Dashboard. Page-level
  action buttons live in the `#view-actions` row underneath it, right-aligned.
- **Stat tiles:** white rounded cards (`--r-card`), soft shadow, a tinted rounded square holding
  a line icon on the left, then big bold number with a **title-case** muted label beneath it
  (not uppercase, not tracked). Laid out as a grid of independent cards with gaps.
- **Pills/badges:** fully rounded (`--r-pill`), small dot + label for status pills
  (`Active`/`Overdue`/`Done`), soft tint background with matching-hue text for class/type tags.
- **Buttons:** `--r-md` radius (not full pill, matching the reference's "Refresh"/"New event
  type" buttons). Primary = solid `--accent-solid` fill, white text. Secondary/ghost = white or
  transparent with a border.
- **Class cards:** white, `--r-card`, `--shadow-card`, hairline border, and a 3px **top** border
  in the class color (not a left bar). Course code is the heading, course name sits under it in
  muted text, then professor, then meeting time and location rows, then a divided "Next due"
  block with the soonest open assignment and a chevron.
- **Tables (grade table, ad-style rows):** uppercase muted column headers on a `--surface-2`
  row, hairline row dividers, hover tint `--surface-2`.
- **Modals:** white, `--r-card`, `--shadow-pop`, no colored top rule (that was the editorial
  pass's "letterhead" device — drop it here).
- **Dashboard (Overview) is a single full-width column.** There is no right rail. The app shell
  is the left sidebar plus `#main`, and nothing else. The mini calendar, the Today panel and the
  motivational quote card that used to sit in a 300px rail were removed at Saif's request, along
  with `#rail`, `railHtml()` and their CSS. Don't reintroduce a persistent side column here:
  everything on Overview stacks in `.dash-row` pairs across the full `#main` width.
  (The **Calendar** page keeps its own in-page right column, `.cal-rail`. That is part of the
  calendar layout, not the app shell, and it still uses the shared `.rail-card`,
  `.mini-cal-head` and `.today-row` classes.)
- **Class page:** a class is a full page (`activeTab === 'class'` + `activeClassId`), not a modal.
  Header = breadcrumb back to Classes, then a colored dot, the course code as `h1`, the grade
  badge, the course name, and the professor. Under that a tab bar (Home / Assignments / Notes /
  Files / Grades) with a `…` overflow menu on the right holding Edit class, Manage syllabus and
  Delete class. Home is a two-column grid: Upcoming / Recent Notes / Recent Files on the left,
  Course Info / Progress / Quick Links on the right.
  `openClassDetailModal(id)` is kept as the name but now just navigates to that page, so every
  older call site that re-opened the modal to refresh still works.
- **Syllabus** has no tab of its own (the mockups don't show one). Syllabus topics drive the
  Progress card on the class Home tab, and are edited in a modal from "Manage syllabus".
- **Assignments page:** All Assignments / By Class segmented tabs, a search box and a dark
  "+ Add assignment" button on the same row, then filter pills (All / Today / This Week /
  Upcoming / Overdue / Completed). The table has sortable headers, row checkboxes with a bulk
  action bar, colored type pills, a clickable status pill that cycles Not started → In progress
  → Completed, weight, and pagination. By Class swaps to collapsible per-class groups with an
  extra Grade column.
- **New / edit assignment is the detail card with editable fields** (`openItemModal`,
  `.aform-*`). It shares the detail card's head, bordered cards and foot, so adding an
  assignment looks like the card you get afterwards: the title is typed at the card's own
  30px on a hairline rule, the class sits under it as one chip per class, and the body is
  four cards — Due (date and time in the `.ac-when` split, plus Today / Tomorrow / Friday /
  Next week shortcuts and Repeat weekly), Type and status, Grading, Details (description
  and steps).
  - **Every small fixed set is chips, never a `<select>`.** Class, type, status and grade
    category are chip rows. Type chips carry the same colour pair as the type pill they
    become, status chips the same pair as the status pill. This is the Notes class-pill
    rule applied again: a dropdown hides both what is picked and what else there was.
  - **No click re-renders the modal.** Each pick moves one `active` class and writes one
    hidden input (`f-class`, `f-type`, `f-status`, `f-category`), which is what keeps the
    ids the save path already reads. Only the grade-category row is redrawn, because
    categories belong to a class, and a class without any says so in place rather than
    collapsing the row.
  - **Weight is disabled, not hidden, while a category carries it**, and its hint changes
    to "Set by the category". Score only appears when editing: a brand-new assignment
    cannot have a mark yet.
  - The title input is `input.aform-title`, not `.aform-title`. Same specificity trap as
    `input.nt-title` in Notes.
- **Work status is three-state.** `item.status` stores `todo` | `in_progress` | `done`; Overdue
  is *derived* from the due date and never stored. Use `workStatusKey(item)` rather than reading
  `status` directly when you need the display state.
- **Calendar:** Month / Week / List. Lectures come from class schedules, assignment due dates
  come from items, and standalone **events** (office hours, study groups) are their own table.
  Its in-page right column holds the selected-day panel and an Event Types legend whose checkboxes filter
  the grid. The month grid fits the window when it can and scrolls when it can't; it must never
  clip.
- **Files:** All Files / By Class / Recent / Starred tabs, class folder cards, and a grid/list
  toggle. Starred lives in `localStorage`, not the database, since it's a per-device convenience.
  - **Navigation is a real tree**: All files → a class → a folder → its subfolders, held in two
    variables (`filesFolder` is the class, `filesSubfolder` the folder inside it) because a
    folder belongs to exactly one class and the pair is what the breadcrumb rebuilds. Browsing
    shows one folder's own files and nothing nested below it; the folder cards carry the
    recursive count.
  - **Searching is global on purpose.** A search scoped to the folder you happen to have open
    is a search that hides the answer, so the query ignores where you are standing and matches
    name, class, folder path and type, word by word.
  - **You can always get out.** The old search replaced the body, hid the folder cards and said
    nothing about how to return. There are now four ways back and all of them are visible: the
    ✕ inside the search box, the Clear search button in the results strip, Escape while the box
    has focus, and the breadcrumb, which stays put. The results strip also says how many
    matched, so "nothing here" and "nothing matches" stop looking the same.
  - **Filters are chips, never a `<select>`** — type and age, both rows always on screen. Same
    rule as the assignment form: a dropdown hides both what is picked and what else there was.
  - **Dragging moves, it never copies.** A file has one home class and one home folder, so
    "where does this live" keeps one answer; what it is *used by* lives in `item_files` and
    survives the move. Every drop target lights up the same way (`.fdrop-over`), whether it is
    a class card, a folder card, a breadcrumb or a folder section inside a class.
  - **Deleting a folder never deletes a file.** Its files and subfolders move up to its parent.
    Losing a folder should cost an organising decision, not a file, and the confirm says so.
- **Grades:** stat tiles (Term GPA, Completed, Graded courses, To Be Graded), a By Course table
  with letter chips tinted by grade tier and progress bars, a By Assignment table, plus Grade
  Distribution and Grade Points cards.
- **Lock In (focus timer)** lives *inside* the Focus page by default, in a normal `.card`, with
  the sidebar and page header still visible. It is built from ordinary Vesta parts: `.seg-tabs`
  for the Focus / Short Break / Long Break modes (each with a per-day count), `.btn` and
  `.btn primary` for controls, a standard `<select>` for the task, and the app's own tokens for
  every color. **The default look is the plain light app background.** Scenes, glow and
  glassmorphism are strictly opt-in.
  - **Full screen** is a button, not the default. With no scene picked it is just the same
    timer on the app background. Picking a scene adds `.has-scene`, which is the only thing that
    switches on the immersive light-on-dark treatment, the glow, and ambient particles.
  - Settings open in a **standard app modal** (not a bespoke drawer): an Appearance section
    (Background, Accent, Ambient effect) and a Timer section (steppers, auto-start toggles,
    alarm with Chime/Bell/Digital). All of it persists in `localStorage`.
  - Backgrounds are CSS gradient scenes plus a Custom image URL, since we can't ship licensed
    artwork. `scene: 'none'` is the default and means "use the app's own background".
  - Starting a session from the Dashboard or an assignment navigates to the Focus page rather
    than taking over the screen.
  - **Steppers are typable.** Every duration control is a real `<input type="number">` sitting
    between the `−` and `+` buttons, so a value can be typed as well as stepped.
  - **No control in the settings modal ever rebuilds the modal.** Steppers, switches, scene and
    ambient pills, accent swatches and alarm sounds all route through `syncFocusSettingsUi()`,
    which repaints from `focusSettings` by toggling classes and attributes on the existing
    nodes. Replacing the modal's HTML threw away the caret, the scroll position and the button
    under the pointer. This mirrors the notes autosave and `renderAsgBody()` rules below.
  - **Nothing in an open panel changes height on a click.** Controls that only become
    irrelevant (the Alarm sound row when Alarm is off) stay in place, dimmed and `disabled`,
    rather than being removed, so the rows below never jump. Only a control that genuinely
    reveals new input (the Custom image URL field) is allowed to add height.
  - **Overlay stacking order:** `.overlay` (modals) `z-index:200` and `.search-overlay` `210`
    both sit above `.fx-overlay` (`120`). Any new full-bleed layer must stay under 200 so
    modals can still open on top of it.
  - **Focus time is measured, not assumed.** Seconds are counted by the ticking timer while it
    is running, so pausing stops the count and wall-clock time since Start means nothing.
    Accrued seconds flush to the linked assignment every minute and on pause, task switch,
    phase change, session end and page hide, so quitting mid-session never loses the time.
    The Focus page reports **Focused today** (all real seconds, task or not, per day in
    `localStorage`) and **Logged to assignments** (the server-side per-item totals). Never
    label a configured setting as if it were measured time.
- **Notes** follows `vesta mockups/notes mockup updated.png` and is a **two-pane**
  workspace: the note list and the document. The class identity moved to the top of
  the list pane as a coloured dot, the course code in bold, and the course name under it.
  - **Notes opens on All Notes**, a real page rather than an empty state: a search box
    across every note in every class, a Pinned and starred strip, then one section per
    class showing its notes as cards. "← All Notes" goes back to it.
  - **Switching class is a row of pills**, one per class, each with its colour dot and
    note count, the current one filled. An earlier version used a small chevron that
    opened a dropdown; it hid which class you were in and how many others existed, and
    Saif asked for it to go. Do not put a primary switch behind an icon.
  - **New note** and **New folder** are blue text actions with a ＋, not filled buttons.
    Filled buttons in that column competed with the note titles for attention.
  - The editor header is one row: breadcrumb on the left (class › folder › note),
    then Saved with a green tick, a blue **Share** button, a `…` menu and the
    full-screen icon. Pin, star, rename, duplicate, history, links and move-to-folder
    all live in the `…` menu so the header stays legible.
  - **Share** exports or copies. It does not send a note to another person, and its
    menu says so, because accounts are not switched on yet.
  - The toolbar is a **single scrolling row** directly under the header, styled as a
    rule rather than a bordered tray, and sits **above** the title. It uses dropdowns
    for paragraph style, font, size, colour, lists and insert, which is what keeps
    roughly thirty commands in one row.
  - The document itself has **no border**: title, meta line, hairline rule, then the
    text. A bordered editor inside a bordered card read as a box in a box.
  - Headings inside a note are navy (`--note-heading`), which separates the
    document's own structure from the app's blue interface accents.
  - Note titles use `input.nt-title`, not `.nt-title`. `input[type="text"]` is more
    specific than a bare class and silently wins otherwise; this cost an hour once.
- Previously **Notes** was a three-pane workspace: class switcher, folder tree, editor.
  Notes have a title, an optional folder, and autosave on a 700ms debounce that patches the note
  and refreshes cached state *without* redrawing the editor, so the caret is never lost.
- **Calendar cells:** rounded (`--r-sm`/`--r-md`), soft border, today = accent border + soft
  accent tint fill, event chips = small rounded (`--r-sm`) tinted pills.

### Headstart

Headstart is the AI surface, and its design job is to make three things obvious before
anything happens: what it is working on, what it will read, and what it will cost.

- The page is **organised by class**, not by tool. Each class is a section: its open
  assignments as cards ordered by urgency (days until due, pulled forward for exams and
  projects), then a strip of study actions and any saved decks and quizzes. Classes
  themselves are ordered by whichever has the most pressing thing in it.
- Class filtering reuses the **`.nt-class-pill` row** from Notes rather than a dropdown,
  for the same reason it was introduced there: a visible set answers both "which one am I
  on" and "what else is there" at a glance.
- The **spend chip** (`.hs-spend`) sits on the filter row and in the workspace header. It
  shows today's spend against the daily cap with a small meter, and is the way into the
  spending settings. Cost is never hidden behind a menu.
- The **workspace** (`.hs-ws`) is two columns: every tool visible at once on the left,
  grouped by intent (Understand it / Plan it / Work on it), and the run on the right —
  what it will read, anything extra to tell it, the Run button with a live price, then the
  output. Ten tools in a dropdown would hide the whole point of the feature.
  - `.hs-ws-body` is a grid with a fixed `height` and `min-height:0` on both columns.
    Without `min-height:0` a grid child refuses to scroll and spills out past the
    rounded bottom of the modal.
  - The swap between "let Vesta choose" and "choose it myself" is `.hs-srcswap`, a small
    accent-coloured text control. As a normal `.btn` it read as a second heading against
    the 11.5px muted source list above it.
- **A refusal is part of the interface, not an error.** A run that would cost more than the
  confirm threshold comes back 409 and renders as `.hs-confirm` with the price and a way
  through; hitting the daily cap comes back 402 and renders the same panel in danger tones
  with a link to raise the limit. Neither is a `window.alert`.
- **The picker only offers material that would actually contribute.** Unreadable files,
  empty notes and empty folders are shown but disabled and labelled, so what you tick is
  what gets read. A folder that overlaps a note you already ticked does not count twice.
- **Quizzes hide their answer key.** `GET /api/quizzes/<id>` omits `answer` and
  `explanation`; they arrive only in the response to a submitted attempt (or explicitly via
  `?answers=1`). A practice test whose answers are sitting in the DOM is not a test.
  Multiple choice and true/false are marked on the server; written answers come back
  unmarked with the expected answer, and the student marks their own, which folds into the
  same attempt.
- Every Headstart panel opened from inside an assignment card carries a
  **← Back to the assignment** button (`hsBackItemId`). Opened from the Headstart page,
  there is nothing to go back to, so there is no button.
- **Flashcards** use SM-2 scheduling. Review is one card at a time, front first, back on
  click, then four grades. Nothing about the schedule is exposed as a number in the UI.

### Inbox (notes and files with no class)

Sometimes a thing has to be written down or saved before there is time to decide
where it goes. The Inbox is that place.

- In the data it is simply `class_id IS NULL` on `notes` and `materials`. `/api/state`
  returns those rows under `unfiled`, and `POST /api/notes` / `POST /api/materials`
  create them. Filing is a `PUT` with `classId`; `classId: null` moves a thing back.
- In the interface it is a **pseudo-class** (`INBOX_ID = '__inbox__'`, `inboxClass()`).
  `classById` returns it, so the Notes workspace, the file library and previews render
  it with no special cases. It is never added to `classes`, so it cannot leak into the
  calendar, grades, class lists or Headstart.
- It is called **Inbox**, not "Unfiled", because inside a class "Unfiled" already means
  a note with no folder. Two things with one name would be a trap.
- It is always a visible destination: a pill at the end of the Notes class row, a
  folder card on Files, an option in the drop zone's "Into" menu, and a
  **Quick note** button on All Notes that opens a new note with the caret in the title.
- The Inbox has no folders (folders belong to a class). Everywhere a note or file is
  shown in full there is a **File under** control listing every class plus the Inbox;
  choosing one moves it and keeps it open in front of you.

### Menus inside the notes header

The Share and … menus close on any click outside them, including bare page. They are
removed from the DOM in place rather than by re-rendering, because a click that
dismisses a menu is often a click into the editor, and a re-render would take the
caret. Clicks on a `<select>` or text inside an open menu do not close it.

### Connections: everything has a home, nothing is trapped in it

A resource is stored once and shows up everywhere it is relevant, so nobody uploads or
files the same thing twice.

- **A file has one home and many uses.** Its home is a class or the Inbox
  (`materials.class_id`). What it is used for lives in `item_files`
  (`item_id`, `material_id`, unique pair), so one rubric or reading can belong to any
  number of assignments. `/api/state` gives every file an `itemIds` list; the older
  single `materials.item_id` column is copied into `item_files` and cleared at startup
  (`move_item_links` in `db.py`), which is safe to run every time.
  - Uploading inside an assignment writes a link. **Attach existing** in the assignment
    card lists files from every class and the Inbox, so reuse never means re-uploading.
  - Detaching removes one link only (`DELETE /api/items/<iid>/files/<mid>`). Moving a
    file to another class or the Inbox keeps its links: home and use are separate.
  - A file attached to an assignment appears in that assignment, in its class's
    **Assignments** section (Files → By Class, and the class page's Files tab), under its
    own category, and in global Files. Rows say where it lives and how many assignments
    use it.
- **Links are suggested, never assumed.** Notes used to be linked silently to any
  assignment whose title appeared in their text. That is gone. `links.py` returns
  suggestions instead: a note that names an assignment or a file, or a file whose text
  names an assignment, gets a dashed `.sugg-chip` with **Link** / **Attach** and a
  dismiss. Dashed, because it is a connection that does not exist until you say so.
  Dismissals are remembered per note in `localStorage`. Titles under four characters are
  never suggested; they match too much.
- **Search reads inside things.** Names are matched in the browser instantly; the words
  inside files and notes are searched on the server (`/api/search`) a moment later and
  land under **Inside files and notes**, with the matching words marked and chips for
  what each hit connects to. Extracted file text never ships in `/api/state`, since
  that would put every PDF in every page load. A match that only hit HTML markup is
  dropped. Note results open the note itself, not just the Notes tab.
- **Headstart reads the assignment's own material first:** its attached files and the
  notes linked to it, then whatever room is left goes to the rest of the class.
- **Readable files:** PDF, Word, and plain text (`.txt .md .csv .tsv .html`). Plain-text
  files uploaded before that was supported are read once at startup.

### Syllabus import

The most important flow in the app: a syllabus becomes a class, its meeting times, its
assignments and exams, and its grading, without the student typing any of it. Nothing
is imported silently.

**Three stages, and nothing touches a class until the last.**
1. **Upload and price** (`POST /api/syllabus`). The file is stored and the read is
   priced before anything is spent: the dialog names the model (Claude Opus 5) and the
   cost, shows today's spend against the daily limit, and makes reading a separate
   click. If it would pass the limit, the button is disabled and "Change the limit"
   returns to the price afterwards. Cancelling deletes the upload.
2. **Read** (`POST /api/syllabus/<id>/read`, `syllabus.py`). The document goes to Opus 5
   as itself (PDF pages keep their tables), constrained by a JSON schema, streamed, with
   `fallbacks: "default"`. Every entry carries a verbatim `evidence` quote and a `sure`
   flag. Server-side, `build_draft` turns the reading into a review draft:
   - week numbers become dates from the first day of term (Week 1 is the week
     containing it); with no weekday, the first class that week that is not a holiday;
   - repeating work becomes one dated item per occurrence, skipping stated gaps and
     no-class days; a weighted repeat becomes its own shared category;
   - SFU's official outline (`outlines.sfu.ca`, free) fills gaps such as the final exam
     slot and professor email, attaches term dates to meetings, and flags disagreements;
   - anything inferred, conflicting or TBA is marked as needing a look, with the reason.
   A read that has been paid for is stored with status `review`, so a reload does not
   lose it: Classes shows "Continue review".
3. **Review and import** (`POST /api/syllabus/<id>/import`, one transaction).

**The review screen** (`#si-root`, a page of its own under dialogs):
- "Here's what Vesta found", summary chips, and any warnings (weights not adding to 100).
- Sections: Course (with SFU disagreements to pick between, colour for a new class),
  Meeting times, Grading (categories with drop rules and a live weight total; the
  letter scale with an on/off), Assignments quizzes and exams, Weekly topics.
- **Unsure rows are amber** with the reason and the syllabus's own words. Each needs
  "Looks right", an edit (editing a flagged field counts as checking it), or unticking.
  Repeating dates can be confirmed as a run. **Import Course stays disabled** while
  anything needs a look, and the server refuses the same way.
- **Import Course is never a disabled button.** A disabled button swallows the click and
  says nothing, which read as "Import does nothing" when a row inside a collapsed group
  still needed a look. With anything left, the button is dimmed (`si-waiting`, not `aria-disabled`)
  but still clickable: pressing it sends nothing, says "Not imported yet: N still need a
  look. Next: ...", expands the group if needed, scrolls to that row and highlights it. The
  footer count is a "show me" link that does the same.
- Typing never redraws the page; clicks that redraw keep the scroll position.

**Re-import into an existing class** compares instead of duplicating. Items are matched
by an `import_key` (normalised title plus type) or title; each shows New, Changed (with
before → after) or Unchanged (hidden by default). Items no longer in the syllabus are
listed unticked. Meeting times, professor and grade scale changes are opt-in. An update
only ever writes title, type, dates, room, category and weight: **never status, score or
subtasks**, and a removal skips anything scored or finished.

**Where it lands:** the class and its meetings (Classes, Calendar), items with dates,
times and rooms (Assignments, Calendar), grade categories and scale (Grades), weekly
topics (the syllabus checklist), the syllabus file (Files → Syllabus), and the term's
name and dates if they were empty.

**Lessons from four real SFU syllabi** (PHIL 110, REM 388, SD 381, IAT 201; each is a
regression fixture in the logic tests). `reconcile` and `build_draft` now guard against
what the reading actually got wrong:
- office hours returned as a class meeting (dropped);
- a grade category that duplicates items already carrying their own weights, or a
  category of one (dropped, so weights are not double counted);
- attendance and participation returned as repeating work (single undated grade);
- schedules whose weeks run Sunday to Saturday (week 1 anchors on the following Monday);
- documents with no year, where weekday arithmetic picked 2020 (dates moved to the
  course's year when the weekday matches, and flagged);
- course code and term missing from the document but present in the file name (taken
  from the file name, with a warning; the file name is also passed to the read);
- a cancelled class does not cancel online work: repeat dates on no-class days are kept
  and flagged, not dropped.
Every flag carries a `flag` kind (`week`, `pattern`, `year`, `noclass`, `model`), and the
review offers one-click confirmation for runs of `week`, `pattern` and `year` dates, since
a week-by-week course can otherwise need 30 separate checks. Word documents are read with
tables in document order; course maps keep nearly everything in tables.

**Vesta lives outside iCloud** (`~/Developer/vesta`, with a shortcut at `projects/vesta`).
iCloud's "optimise storage" was offloading source files, the page and uploads, which made
reads stall for minutes, and a live SQLite database in a syncing folder risks corruption.

**Structured-output limits (found against the live API, both rejected for free with a 400):**
at most 16 nullable or union-typed fields per schema, and a cap on the compiled grammar
that enums and nested objects inflate. So the syllabus schema has no nullable fields
("" and 0 mean not stated), no enums (allowed values are in descriptions), and a repeat's
details are flat `repeat_*` fields on the assessment. `normalize_raw` turns placeholders
back into `None` and tidies free text ("Monday" to Mon, "Midterm" to exam). Before
changing the schema, probe it with a one-line document and `max_tokens: 16` (about 1¢).

**Grade categories.** `grade_categories` (name, weight, drop_lowest); an item with
`category_id` shares the category's weight instead of carrying its own. The standing is
each category's average after dropping the lowest N (never all), counted in proportion
to how much of it has been marked. Classes have a Grade categories editor, and the item
form offers the class's categories.

**Cost accounting** prices each call by the model that ran it (`MODEL_PRICES` in
`ai.py`), so an Opus read is not counted at Sonnet's rate.

### Schedule from SFU

SFU publishes every section's meeting days, room, term dates and final exam slot for
free, with no login, at the same outlines API syllabus import already uses. That makes
it the cheapest source of truth in the app: no AI call, no upload, no cost. It fills the
gap syllabus import cannot, and vice versa. SFU knows the registrar's timetable but not
the assignments; the syllabus knows the assignments but often says "TBA" for rooms.

**The one question it has to ask is which section is yours.** A course is either several
independent lectures (CMPT 120: D100/D200/D300) or one lecture with labs and tutorials
hanging off it (IAT 201: B100 plus four labs; PHIL 110: D100 plus eight tutorials).
`associatedClass` in SFU's section list is what separates those, so the labs offered are
only ever the ones attached to the lecture actually chosen. A course with a single
section (CMNS 130) is never asked about at all: `needsChoice` is false and the pull runs
straight through.

Every option is a visible chip, never a dropdown. Choosing your lab is recognition, not
lookup, and a `<select>` hides seven of the eight tutorials until you open it.

**Two guards, both learned the hard way.**
- *No class is preselected.* Defaulting to the first class meant a timetable could be
  drafted for one course and written into another, and the review screen would then
  offer to delete that other class's real lectures for the crime of not appearing in
  SFU's outline. Caught by looking at a screenshot, not by a passing DOM test.
- *A code that does not match the chosen class is refused* (`calSameCourse` in the page,
  `same_course` in `calsync.py`). Server-side the mismatch also suppresses matching
  entirely: existing meetings are neither matched nor offered for removal, and the draft
  carries a warning saying so. An unparseable code on either side never blocks.

**The flow** mirrors syllabus import on purpose, because the student already knows it:
pick class and code, pick section(s), then a review screen (`#si-root`, sharing the
syllabus review's furniture) headed "Here's what SFU has for …". Rows are ticked, not
flagged: SFU is the registrar, so nothing it says needs a look. Nothing reaches a class
until **Add to class**, which runs in one transaction. Ticking a row repaints only the
footer, so the page never moves under the cursor.

**One pending draft per class.** `calendar_imports.class_id` exists so pulling again
replaces the previous unapplied draft instead of stacking another banner.

**Times are padded on the way in.** SFU says `9:30`; Vesta stores `09:30`. Unpadded
times compare unequal and sort wrong, which would have made matching silently miss.

**Endpoints** (`calendar_api.py`): `GET /api/calendar/sfu/sections`,
`POST /api/calendar/sfu/draft`, `GET|DELETE /api/calendar/imports/<id>`,
`POST /api/calendar/imports/<id>/apply`.

**What SFU cannot give you.** IAT 201 B100 publishes no meeting days at all, because it
is the online enrolment section; the pull returns the lab slot and a warning naming what
was skipped. Exams are published partway through term, so early in a semester
`examSchedule` is simply absent. Finals live in `examSchedule`, never as `isExam` rows
in `courseSchedule` (checked across 16 sections in fall 2025 and spring 2026); the
fallback reading `courseSchedule` is a guard that has never fired.

### Calendar sync (in progress)

`ics.py` reads iCalendar text with no new dependency, since Vesta already writes it by
hand and `icalendar` would drag a package into a Python 3.9 venv that is past end of
life. It handles folding, `VALUE=DATE`, `TZID`, UTC, `EXDATE`, and weekly/daily `RRULE`.
Two details it gets right that are easy to get wrong: **`COUNT` limits what the rule
generates and `EXDATE` removes occurrences afterwards** (filtering first would invent a
replacement for every excluded date, pushing a term of deadlines one meeting later), and
every bound is coerced onto DTSTART's footing before comparison, because dates, naive
datetimes and aware datetimes do not compare.

`export_ics` now emits `DTSTART;TZID=America/Vancouver` with a `VTIMEZONE` block instead
of floating times. Floating times meant a weekly class drifted an hour when daylight
time ended in November. The transition markers inside `VTIMEZONE` stay bare, as the spec
requires.

**Schema:** `calendar_accounts` (one connected calendar), `sync_links` (any local object
to its remote counterpart, holding a hash of each side as it stood at the last
successful sync, which is what tells a one-sided edit from a real conflict), and
provenance columns on `events` so a synced event is never mistaken for a typed one.

**Google (`gcal.py`, mapping done, transport untested).** Plain `httpx` (already present
with `anthropic`) is the whole dependency: Calendar is a REST/JSON API and an OAuth
refresh is one POST, so `google-auth` and `googleapiclient` are not needed. The module is
split so everything with judgement in it is pure and testable with no network and no
credential, and the part that needs a credential has almost no logic in it.

The file is `gcal.py`, not `google.py`: a module named `google` in the project root
shadows the `google` namespace package, which would break `google.protobuf` and friends
the day anything pulls one in.

**Google's contract, verified against the docs rather than recalled:**
- **All-day `end.date` is exclusive.** A one-day event on the 15th ends on the 16th.
  Equal start and end is a zero-length event that readers drop or place wrongly. This
  was also a live bug in `export_ics`, now fixed the same way.
- **`recurrence` takes RRULE lines only**; Google rejects `DTSTART`/`DTEND` inside that
  field. The start comes from the event's own `start`, and the rule says only how it
  repeats.
- **A stale `syncToken` returns 410**, which means wipe the local store and do a full
  sync again, not retry. `GoogleError.resync` carries that signal up.
- **`access_type=offline` plus `prompt=consent`** is what actually yields a refresh
  token; without it you get an access token that dies in an hour.

**Two timestamp traps, both found by testing rather than by reading.** Google returns a
`dateTime` carrying the calendar's offset most of the time, but an event written by
another client comes back as UTC with a trailing `Z`. Slicing that string reads
`2026-10-16T03:59:00Z` as 03:59 local and moves the deadline a day late, so `_local`
parses and converts (and must rewrite `Z` to `+00:00`, which 3.9's `fromisoformat`
requires). The same instant can therefore arrive in two spellings, so `remote_hash`
normalises timestamps before fingerprinting: hashing raw strings would report a conflict
on an event nobody touched, and normalising is also what lets a body Vesta is about to
send and one Google sent back compare at all.

**Conflict rule:** `resolve` compares each side against what it looked like when the two
last agreed (`sync_links.local_hash` / `remote_hash`). Only a genuine two-sided change is
a conflict; Vesta keeps its version and flags the difference rather than silently
overwriting in either direction. `etag` is deliberately not used for this, since it moves
on every write including Vesta's own.

**What gets pushed, and what happens to finished work.** Deadlines and exams only
(`should_push`: anything with a due date). Class meetings are deliberately left out;
five courses of recurring lectures is a wall of repeating blocks in a calendar that
usually already has them, and the job of Google here is answering "what is due".
`meeting_body` stays, tested and unused, for the day that changes. Work marked done
keeps its event and gains a ✓ rather than being deleted, so the calendar remains a
record of the term. Completion changes the fingerprint, which is what makes the tick
actually get pushed.

**The redirect URI is derived from the address being browsed, not hard-coded.** Vesta's
port is not fixed, because **macOS Control Center permanently occupies 5000** (it is the
AirPlay Receiver), so the app usually runs on 5055 or whatever else is free. A hard-coded
`localhost:5000` fails with `redirect_uri_mismatch` at the worst possible moment, which
is why `redirect_uri()` reads `request.host` and only trusts loopback hosts: Google
rejects a raw LAN IP as a redirect target, so reaching Vesta from a phone falls back to
the configured default. Register every port you actually use as an Authorised redirect
URI; Google accepts several. `GOOGLE_REDIRECT_URI` pins one if you would rather.

The client type is **Web application**, and plain HTTP on localhost is allowed. This was
worth checking rather than assuming: a web search
claimed Google blocks plain-HTTP localhost from January 2026, but Google's own web-server
documentation says "Redirect URIs must use the HTTPS scheme, not plain HTTP. Localhost
URIs (including localhost IP address URIs) are exempt from this rule." The loopback
deprecation that search was half-remembering applies to iOS, Android and Chrome client
types, not to web ones. Paths are unrestricted apart from `/..` traversal and `#`
fragments.

**No session, so the OAuth `state` lives in the database.** The local app has no
`secret_key`, no cookies and no sessions at all, so there is nowhere in a request to
keep a CSRF token between the redirect out and the callback back. It goes in
`app_settings` under its own key, the same JSON key/value pattern `ai.py` uses.

**`gsync.py` decides, the routes execute.** `plan_push` compares only the local side,
because the remote hash is not knowable without a pull; `plan_pull` is where a genuine
two-sided change is detected and the link marked `conflict`, after which `plan_push`
refuses to clobber it and reports it instead. An event Google has that Vesta never
created is offered for review rather than written, since a calendar full of dentist
appointments is not coursework.

**The routes** (`calendar_api.py`): `GET /api/calendar/google/status` (what Settings
renders from), `GET /api/calendar/google/start` (builds the consent URL and stores the
state), `GET /api/calendar/google/callback` (always redirects into the app, never
returns JSON, so a failure lands somewhere a student can read), `POST
/api/calendar/google/sync`, `DELETE /api/calendar/google`.

**Sync pulls before it pushes.** A change made on both sides has to be known before
anything is written, or Vesta would overwrite a change it had not noticed yet. A 410 on
the sync token clears the stored remote hashes and does one full pass. Disconnecting
forgets the account but deliberately leaves the events in Google: deleting a student's
calendar entries because they unlinked an app is not ours to do.

**What is verified and what is not.** The mapping (`gcal.py`, 16 checks), the planner
(`gsync.py`, 17 checks against the real schema), every route's behaviour without
credentials including the callback's CSRF check, and both Settings branches in light and
dark. **The transport has never executed**: no request has been made to Google, because
that needs credentials only the account owner can create. The first real connection is
the first time that code runs.

**Setup.** `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env` (placeholders are
already there, and the loader ignores empty values so nothing reports itself configured
until they are filled). Google Cloud project, Calendar API enabled, consent screen
**published to Production**, OAuth client of type **Web application**. The Calendar API
is free well past any student's usage; an unverified app shows a warning screen once and
is capped at 100 users, which is far more than this needs.

**Friends are a different product.** Local Vesta has no authentication of any kind: no
login, no sessions, no `user_id` column anywhere, no users table, and it binds
`0.0.0.0`. Sharing its address shares everything. Multi-user is what `cloud/` is for,
and its row-level security is sound (`002_rls.sql` applies owner-only policies to all 17
tables through `apply_owner_rls`, with `force` RLS, `WITH CHECK` on updates so a row
cannot be reassigned, `anon` granted nothing, and `allowed_emails` invisible to every
client role). But the cloud schema is well behind the local app, so Google sync is being
finished locally first rather than built twice against a moving target.

### Settings

The Sidebar card in Preferences can hide Grades and Focus from the nav. Only the button
goes; the pages keep working and stay reachable from ⌘K search (every page is listed
there), the dashboard's Current Average tile, and Start Focus Session.

Follows `reference/settings inspo.png`: sections down the left, each with an icon and a
one-line summary, and each section a stack of `.st-card` cards on `--bg`, so the window
reads like the app (nav column beside content). The body has a fixed height so switching
sections never makes the window jump. Card and grid classes are `.st-*`, not `.set-*`,
because `.set-card` and `.set-grid` already belong to flashcard sets on the Study page.

**Settings only holds what cannot be set anywhere else.** If a page has its own control
for something (calendar view and detail, Files layout, Focus, Headstart spend, the
sidebar's collapsed state), that control is where it lives and it remembers the choice
itself. It does not get a second copy in Settings. Four sections: Account (profile,
password, this device, export), Semester (terms, default grading scale), Preferences
(theme, which optional pages the sidebar shows, note editor, notifications), Integrations (Google Calendar). The mockup's
eleven tabs, 2FA, photo upload and delete account are left out: the tabs would be
duplicates and the rest is not built, and a button for a missing feature is worse than
no button.

## Class color palette

Friendly, saturated, distinct hues (not muted "ink" tones) — this is what reads as "bubbly"
against white cards:

```
#E0693C  #1DA6A0  #9B5DE0  #8AA62E  #E0568F  #D9A017  #3576D9  #34A868
```

## What NOT to do here

- No serif type, anywhere, for any reason.
- No zero-radius rectangles as a structural device. No hairline-rule section dividers standing
  in for card boundaries.
- No uppercase treatment on page-level `h1`s (that reads as a masthead/document, not a product
  UI). Uppercase micro-labels are fine (see Typography); shouting page titles are not.
- Don't reuse `--accent` as a solid button background — use `--accent-solid` (see above).

"""Canvas: reading a course off Canvas and turning it into Vesta's rows.

Saif asked for a browser extension that would push Canvas files into his classes. This
is that feature without the extension: SFU lets a student mint a personal access token
(Account -> Settings -> New Access Token), so Flask can read Canvas directly, which
works on his phone, can run on a schedule, and is one codebase rather than three.
`CANVAS.md` has the full reasoning and the rest of the plan.

Split the same way `gcal.py` is. Everything above "the transport" is pure: it turns
Canvas JSON into the fields Vesta's tables want, and is tested with no network and no
token. The client below is the only part that talks to Canvas and has almost no
judgement in it.

Two things about Canvas shaped the design, and both are easy to get wrong:

* **a due date is UTC.** An assignment due 11:59pm Tuesday in Vancouver arrives as
  `2026-09-30T06:59:59Z`, which is Wednesday. Splitting that string on "T" files every
  assignment a day late, so `due_local` converts before it splits.
* **a course can switch its Files tab off**, and then `/files` answers 403 rather than
  an empty list. The files are still there, reachable through the modules, so
  `course_files` falls back to walking `/modules` and never reports the 403 as a
  failure.

No new dependency: `httpx` is already here for `gcal.py` and `auth.py`.
"""
import re
import urllib.parse

from datetime import datetime

from ics import zone           # the guarded ZoneInfo lookup, None where tzdata is not

API = "/api/v1"

# Canvas caps per_page at 100 for most endpoints and silently clamps anything larger.
PER_PAGE = 100

# A stop on pagination. A term of one student's courses is a handful of pages; fifty
# means something is looping, and an unbounded `while next_link` would follow it.
MAX_PAGES = 50

# Our own ceiling on a server-side download. `MAX_CONTENT_LENGTH` does not apply here
# because nothing is posted through Flask, and the Railway volume is the small, single
# disk that everything shares (see the parked object-storage note in NEXT.md).
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

# What counts as a graded submission. Anything else has a null score, or a score that
# is not his yet, and must not land in the Grades tab as a zero.
GRADED = "graded"

# The vocabulary for guessing an item's type from its title or its Canvas group name.
# Ordered, because "final exam" must be read as an exam before "exam" is even reached,
# and a "reading quiz" is a quiz rather than a reading. The words and the types they
# map to deliberately mirror `syllabus.TYPE_ALIASES`, so that an assignment imported
# from Canvas and the same assignment read off a syllabus PDF get the same type;
# `tests/test_canvas.py` asserts that every type produced here is one `syllabus.py`
# knows. It is not imported from there because that module pulls in the whole AI layer
# and this one is meant to stay cheap to import and trivial to test.
TYPE_WORDS = [
    ("final exam", "exam"), ("midterm exam", "exam"), ("final", "exam"),
    ("midterm", "exam"), ("exam", "exam"), ("test", "exam"),
    ("quiz", "quiz"),
    # "read" earns its place from real data: a whole course of Saif's names its
    # readings "Read Noba Attention & Failures of Awareness", which matched nothing and
    # came through as a plain assignment. It sits after "quiz" and "exam" on purpose,
    # so a "reading quiz" stays a quiz.
    ("reading response", "reading"), ("reading", "reading"), ("read", "reading"),
    ("discussion post", "discussion"), ("discussion", "discussion"),
    ("project", "project"), ("presentation", "project"),
    ("lab", "assignment"), ("essay", "assignment"), ("paper", "assignment"),
    ("report", "assignment"), ("homework", "homework"), ("assignment", "assignment"),
    ("participation", "other"), ("attendance", "other"),
]


class CanvasError(Exception):
    """A failure worth showing the student, in Canvas's own words where we have them."""

    def __init__(self, message, status=None, needs_token=False, rate_limited=False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.needs_token = needs_token      # a 401: the token is wrong, expired or gone
        # a 403 that is a throttle rather than a permission. Canvas uses the same
        # status for both, and they must not be confused: see `_get`.
        self.rate_limited = rate_limited


# ---------------------------------------------------------------------------
# the pure part
# ---------------------------------------------------------------------------

def normalise_host(value):
    """'https://canvas.sfu.ca/courses/12345' -> 'canvas.sfu.ca'.

    Nobody types a bare hostname. What gets pasted into a settings field is whatever
    was in the address bar, so accept a full URL, a trailing slash, a scheme on its own
    or the host alone, and keep only the host. A stored value of
    "https://canvas.sfu.ca/" would otherwise build request URLs with a doubled slash,
    which Canvas answers with a redirect our client does not follow.
    """
    v = (value or "").strip()
    if not v:
        return ""
    if "//" not in v:
        v = "https://" + v
    host = (urllib.parse.urlsplit(v).netloc or "").strip().lower()
    return host.rstrip("/")


def plain_text(html):
    """The HTML body of a Canvas assignment as something `items.notes` can hold.

    `ai.strip_html` does exactly this and is imported rather than repeated, but lazily:
    importing `ai` pulls in the Anthropic client and the whole generation layer, and a
    module whose job is reading JSON should not cost that at import time.

    The one thing added on top is closing up the space a stripped tag leaves in front
    of punctuation. Canvas descriptions are written in its rich text editor and are
    full of inline tags, so "Covers <b>weeks 1 to 5</b>." arrives as "... 5 ." without
    this.
    """
    from ai import strip_html
    return _pg_safe(re.sub(r"\s+([.,;:!?)\]])", r"\1", strip_html(html)))


def _pg_safe(text):
    """Text Postgres will store. It refuses a NUL byte outright, and the whole apply
    then fails, which is how a single odd character in one instructor's description
    would stop a sync that works everywhere else. SQLite accepts it, so only the
    deployment would ever find out. Uploads get the same guard in `app.db_safe_text`."""
    return (text or "").replace("\x00", "")


def due_local(due_at, tz=None):
    """A Canvas UTC timestamp -> (date, time) as Canvas shows it, as Vesta stores them.

    Returns ('2026-09-29', '23:59') for an assignment due at the end of that Tuesday,
    which Canvas reports as '2026-09-30T06:59:59Z'. Converting first is the whole point
    of this function: the naive split puts every evening deadline on the following day.

    `tz` is the time zone of the student's Canvas account, and it matters more than it
    looks. Canvas shows Saif his deadlines in America/Los_Angeles. The time zone
    database says America/Vancouver leaves daylight saving for good on 2026-11-01, so
    converting in Vancouver's zone put every deadline from November to March an hour
    after the one Canvas showed him: Discussion 08, due 23:59 on Nov 15 in Canvas,
    arrived as 00:59 on Nov 16, and 45 REM 388 deadlines showed as "moved". Converting
    in the account's own zone reproduces Canvas's screen exactly, and where the two
    clocks disagree it is the earlier of the two, which is the safe one to be told.
    Without it, `ics.zone()` is the fallback.

    Seconds are dropped, because `items.due_time` is 'HH:MM'. A Canvas deadline of
    23:59:59 is stored as 23:59 rather than rounded up into the next day.
    """
    raw = (due_at or "").strip()
    if not raw:
        return None, None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    tz = tz or zone()
    if tz is not None and dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def item_type(assignment, group_name=""):
    """Which of Vesta's item types this Canvas assignment is.

    The title is asked first and the group name second. A course that files everything
    under a group called "Assignments" still names its midterm "Midterm 1", while a
    course with a group called "Quizzes" names its quizzes "Week 3", so between the two
    the more specific answer usually comes from whichever mentions a kind of work at
    all. Canvas's own `submission_types` is the last resort: it distinguishes a quiz and
    a discussion from everything else, and calls the rest "assignment".
    """
    for text in ((assignment or {}).get("name") or "", group_name or ""):
        found = _type_from_text(text)
        if found:
            return found
    subs = (assignment or {}).get("submission_types") or []
    if (assignment or {}).get("quiz_id") or "online_quiz" in subs:
        return "quiz"
    if "discussion_topic" in subs:
        return "discussion"
    return "assignment"


def _type_from_text(text):
    """The first known kind of work named in a title or a group name.

    Plurals are allowed for, because that is how Canvas group names read: the groups
    are "Quizzes", "Exams" and "Readings" while the assignments inside them are
    "Quiz 3" and "Midterm 1". Matching only the singular found nothing in exactly the
    place this fallback exists to read. 'zes' is in the list for "quizzes".
    """
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    if not t:
        return None
    for word, typ in TYPE_WORDS:
        if re.search(r"\b" + re.escape(word) + r"(?:s|es|zes)?\b", t):
            return typ
    return None


def score_percent(submission, points_possible):
    """His score on this assignment as a percent, or None if he does not have one yet.

    Vesta stores a percent, not points: the grade maths in the frontend is
    `weight * score / 100`, so a raw Canvas score of 18 would be read as 18%.

    None is returned generously. A submitted but unmarked assignment, an excused one
    and an ungraded survey all have to come back empty, because a zero here is
    indistinguishable from a real zero once it is in the table, and it would drag a
    calculated grade down for weeks without an obvious cause.
    """
    s = submission or {}
    if s.get("excused") or (s.get("workflow_state") or "") != GRADED:
        return None
    score = s.get("score")
    try:
        total = float(points_possible)
        score = float(score)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return round(score / total * 100, 2)


def counts_for_grade(assignment):
    """Whether this assignment is part of the final grade at all.

    Canvas offers `omit_from_final_grade` for the practice quiz that does not count,
    and an unpublished assignment is not visible to students and may never happen.
    Neither should take a share of a weight, because every other assignment's weight is
    computed from what is left.
    """
    a = assignment or {}
    if a.get("omit_from_final_grade"):
        return False
    if a.get("published") is False:
        return False
    return True


def weighting(course, groups):
    """How this course's grade is built: weighted categories, or raw points.

    Canvas has two modes and the field that distinguishes them is on the *course*, not
    the group. With `apply_assignment_group_weights` on, each group carries a
    `group_weight` percent and Vesta can mirror it exactly: a Canvas assignment group
    is a Vesta `grade_categories` row, and Vesta already shares a category's weight
    across the items in it. Nothing has to be divided up here.

    With it off, the course is marked out of total points and every `group_weight` in
    the response is meaningless (usually 0, sometimes a leftover from a previous
    setup). Importing those would silently wreck the Grades tab, so that branch builds
    no categories and gives each assignment its own share of 100% by points.

    Two things learned from Saif's actual courses shape this:

    * **an empty group is not a category.** One of his courses carries "Imported
      Assignments" and "Unused assignments for this offering", both at 0% with nothing
      in them, which are the instructor's leftovers and not part of anyone's grade.
      A group with nothing that counts towards the grade is skipped.
    * **a group holding one assignment is that assignment's weight**, not a category.
      Another of his courses files each of its four assignments in a group of its own
      and ends up with eleven groups, seven of them holding a single item. Vesta shares
      a category's weight across its items, so a one-item category and a plain weight
      behave identically; the flat version is simply what the Grades tab can be read.

    Returns (categories, item_weights). Both can be non-empty, but never for the same
    assignment: an item carries its own weight or its category's, never both.
    """
    weighted = bool((course or {}).get("apply_assignment_group_weights"))
    if weighted:
        cats, singles = [], {}
        for g in groups or []:
            counted = [a for a in (g.get("assignments") or []) if counts_for_grade(a)]
            if not counted:
                continue
            weight = _number(g.get("group_weight")) or 0.0
            if len(counted) == 1:
                singles[counted[0].get("id")] = weight
                continue
            cats.append({
                "canvasId": g.get("id"),
                "name": (g.get("name") or "").strip() or "Assignments",
                "weight": weight,
                # Canvas keeps its drop rules per group, and Vesta has the same feature.
                "dropLowest": int((g.get("rules") or {}).get("drop_lowest") or 0),
            })
        return cats, singles

    graded = [a for g in (groups or []) for a in (g.get("assignments") or [])
              if counts_for_grade(a)]
    total = sum(_number(a.get("points_possible")) or 0.0 for a in graded)
    if total <= 0:
        # Every assignment is worth zero points, or none of them says. There is no
        # honest weight to give any of them, and a made-up one is worse than none.
        return [], {}
    return [], {a.get("id"): round((_number(a.get("points_possible")) or 0.0) / total * 100, 4)
                for a in graded}


def _number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def import_key(assignment_id):
    """What makes a re-sync update an assignment instead of duplicating it.

    `items.import_key` already exists for exactly this, and `syllabus_import.py`
    matches on it, so Canvas needs no column of its own. The `canvas:` prefix keeps
    these from ever colliding with the title-and-type keys the syllabus importer
    writes.
    """
    return "canvas:" + str(assignment_id)


def plan_course(course, groups, tz=None):
    """Everything Canvas knows about one course, in the shape the sync engine applies.

    Deliberately the same two keys the syllabus importer's draft uses, `categories` and
    `items`, so that the review screen Saif already has (what is new, what changed, and
    a tick per change) can show a Canvas sync without a second conflict mechanism being
    invented. Canvas wins where Vesta has nothing; where the two disagree, that screen
    shows both and leaves the tick off.

    `weightsFromCanvas` says whether the weights in here are worth trusting, and the
    review screen should leave every weight tick off when it is False. Saif's two
    points-based courses are why: one has twenty-five assignments all worth zero
    points, so every derived weight is 0%, and the other has five assignments set up
    out of a term's worth, so the first one comes out at 62.5% of his grade. Both
    numbers are arithmetically correct and both are wrong about the course. Where
    Canvas does use weighted groups the figures are the real ones and can be ticked on.
    """
    weighted = bool((course or {}).get("apply_assignment_group_weights"))
    cats, item_weights = weighting(course, groups)
    by_canvas_id = {c["canvasId"]: c for c in cats}
    items = []
    for g in groups or []:
        group_name = (g.get("name") or "").strip()
        cat = by_canvas_id.get(g.get("id"))
        for a in (g.get("assignments") or []):
            points = _number(a.get("points_possible"))
            due_date, due_time = due_local(a.get("due_at"), tz)
            items.append({
                "canvasId": a.get("id"),
                "importKey": import_key(a.get("id")),
                "title": _pg_safe((a.get("name") or "").strip()) or "Untitled",
                "type": item_type(a, group_name),
                "dueDate": due_date,
                "dueTime": due_time,
                "notes": plain_text(a.get("description")),
                "url": a.get("html_url") or None,
                "points": points,
                # A category carries the weight when the course is weighted; otherwise
                # the item carries its own. Never both, or the grade is counted twice.
                "categoryCanvasId": cat["canvasId"] if cat else None,
                "weight": None if cat else item_weights.get(a.get("id")),
                "score": score_percent(a.get("submission"), points),
                "countsForGrade": counts_for_grade(a),
                # Kept for later: a rubric on the assignment is the same thing the
                # rubric parser currently spends an AI call extracting from a PDF.
                "rubric": a.get("rubric") or None,
            })
    return {"categories": cats, "items": items, "weightsFromCanvas": weighted}


def course_title(course):
    """A course's name without the code Canvas repeats at the front of it.

    Canvas names a course "PHIL110 D100 Introduction to Logic and Reasoning", and every
    place that shows the name already shows the code beside it.
    """
    c = course or {}
    code = (c.get("course_code") or "").strip()
    name = (c.get("name") or "").strip()
    if code and name.lower().startswith(code.lower()):
        name = name[len(code):].strip(" -:")
    return name


def class_code(course):
    """The code a Vesta class made from this course should carry: "PHIL 110".

    Canvas's course code carries the section ("PHIL110 D100", "SD381 OL01"), which is
    noise on a class card, and runs the subject into the number. Laid out the way a
    student writes it instead. Anything that does not look like subject-plus-number is
    kept as Canvas had it rather than guessed at.
    """
    raw = ((course or {}).get("course_code") or "").strip()
    m = re.match(r"^(?:SFU[-\s]*)?([A-Za-z]{2,6})[-\s]*0*(\d{2,4}[A-Za-z]?)\b", raw)
    if not m:
        return raw
    return m.group(1).upper() + " " + m.group(2).upper()


def course_label(course):
    """What to call a Canvas course on screen."""
    c = course or {}
    return (c.get("course_code") or c.get("name") or "").strip() or ("Course " + str(c.get("id")))


def _code_key(value):
    """'CMPT 120 D100' and 'SFU-CMPT-120' both -> 'cmpt120'.

    Section suffixes are dropped on purpose. Canvas course codes carry the section
    (D100, E200) and Vesta class codes usually do not, so comparing them literally
    matches nothing. The trailing letter-and-digits group goes only when a subject and
    a number have already been found, so a code that is nothing but a section is left
    intact rather than emptied.
    """
    t = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    m = re.match(r"^(?:sfu\s+)?([a-z]{2,6})\s*0*(\d{2,4})\w*", t)
    if m:
        return m.group(1) + m.group(2)
    return re.sub(r"\s+", "", t)


def suggest_mapping(courses, classes):
    """Pair each Canvas course with a Vesta class, where the course codes agree.

    Only an unambiguous match is suggested: a code that two classes share suggests
    nothing, because a silent wrong pairing files a whole term of materials under the
    wrong class and is tedious to unpick. Everything unmatched comes back with
    `classId` None for the mapping screen to ask about.
    """
    by_code = {}
    for c in classes or []:
        key = _code_key(c.get("code") or c.get("name"))
        if key:
            by_code.setdefault(key, []).append(c)
    out = []
    for course in courses or []:
        key = _code_key(course.get("course_code") or course.get("name"))
        hits = by_code.get(key) or []
        out.append({
            "courseId": course.get("id"),
            "label": course_label(course),
            "name": course_title(course),
            "term": ((course.get("term") or {}).get("name") or "").strip(),
            "classId": hits[0].get("id") if len(hits) == 1 else None,
        })
    return out


def files_from_modules(modules):
    """The files a course's modules point at, for when the Files tab is switched off.

    A module item of type 'File' carries the file's id in `content_id`. The module's
    own name comes back with it because it is better foldering than the Canvas folder
    tree: "Week 3" is where the student looks for it, and a course that hides its Files
    tab often has no folder structure worth importing anyway.

    Order is preserved and duplicates are dropped, since the same reading is commonly
    linked from two modules and should not become two rows.
    """
    out, seen = [], set()
    for m in modules or []:
        module_name = (m.get("name") or "").strip()
        for it in (m.get("items") or []):
            if (it.get("type") or "") != "File":
                continue
            fid = it.get("content_id")
            if fid is None or fid in seen:
                continue
            seen.add(fid)
            out.append({"fileId": fid,
                        "title": (it.get("title") or "").strip(),
                        "module": module_name})
    return out


def next_link(link_header):
    """The `rel="next"` URL out of a Canvas Link header, or None on the last page.

    Canvas paginates every list endpoint this way and the header is the only reliable
    end marker: a full page is not proof there is another, and an empty one is not
    proof there was a previous. The `per_page` we asked for rides along in the URL, so
    following it verbatim is correct.
    """
    for part in (link_header or "").split(","):
        bits = part.split(";")
        if len(bits) < 2:
            continue
        url = bits[0].strip()
        if not (url.startswith("<") and url.endswith(">")):
            continue
        for attr in bits[1:]:
            if attr.strip().replace(" ", "").lower() in ('rel="next"', "rel=next"):
                return url[1:-1]
    return None


# ---------------------------------------------------------------------------
# the transport
# ---------------------------------------------------------------------------

class Client:
    """The thin part. Everything with judgement in it lives above."""

    def __init__(self, host, token, timeout=25):
        self.host = normalise_host(host)
        self.token = (token or "").strip()
        self.timeout = timeout
        if not self.host:
            raise CanvasError("Vesta needs your Canvas address, for example canvas.sfu.ca.")
        if not self.token:
            raise CanvasError("Vesta needs a Canvas access token.", needs_token=True)

    @property
    def base(self):
        return "https://" + self.host + API

    def _headers(self):
        return {"Authorization": "Bearer " + self.token, "Accept": "application/json"}

    def _get(self, url, params=None):
        import httpx
        try:
            r = httpx.get(url, headers=self._headers(), params=params,
                          timeout=self.timeout, follow_redirects=True)
        except Exception as e:
            raise CanvasError("Could not reach Canvas at %s: %s" % (self.host, e))
        if r.status_code == 401:
            raise CanvasError(
                "Canvas rejected the access token. Make a new one under Account -> "
                "Settings -> New Access Token and paste it in again.",
                401, needs_token=True)
        if r.status_code == 403 and _is_throttle(r):
            # Canvas answers a throttled request with 403, the same status it uses for
            # "you may not see this". Telling them apart matters more here than
            # anywhere else: `course_files` reads a plain 403 as "the Files tab is off"
            # and falls back to the modules, so a throttle read as a permission would
            # quietly report a course as having no files rather than saying it gave up.
            raise CanvasError(
                "Canvas is rate limiting Vesta. Wait a minute and sync again.",
                403, rate_limited=True)
        if r.status_code >= 400:
            raise CanvasError("Canvas said no: " + _detail(r), r.status_code)
        return r

    def _paged(self, path, params=None):
        """Every page of a list endpoint, flattened.

        Follows the Link header rather than counting pages, and stops at MAX_PAGES so a
        server that keeps offering a next link cannot spin here forever.
        """
        q = dict(params or {})
        q["per_page"] = PER_PAGE
        url, out, pages = self.base + path, [], 0
        while url and pages < MAX_PAGES:
            r = self._get(url, params=q)
            q = None                      # the next link already carries the query
            body = r.json() if r.content else []
            if isinstance(body, dict):
                # An endpoint that answers with an object rather than a list is not
                # paginated; returning it alone is more useful than a one-item list.
                return body
            out.extend(body)
            url, pages = next_link(r.headers.get("link") or r.headers.get("Link")), pages + 1
        return out

    def profile(self):
        """His Canvas profile, for `time_zone`: the zone Canvas shows his deadlines in."""
        return self._get(self.base + "/users/self/profile").json()

    def whoami(self):
        """Check a token before anything is stored under it, and get his name."""
        return self._get(self.base + "/users/self").json()

    def courses(self):
        """His active courses, newest term first where Canvas says which term.

        `state[]=available` leaves out courses that are still unpublished, which a
        student can be enrolled in weeks before they open and which have nothing in
        them worth importing.
        """
        return self._paged("/courses", {"enrollment_state": "active",
                                        "state[]": "available",
                                        "include[]": ["term", "total_scores"]})

    def course(self, course_id):
        """One course, for `apply_assignment_group_weights`.

        Worth its own call: the flag decides whether this course's grade is weighted
        categories or raw points, and the list endpoint does not always include it.
        """
        return self._get(self.base + "/courses/%s" % course_id).json()

    def assignment_groups(self, course_id):
        """Assignments, their group weights and his own scores, in one pass.

        The three `include[]` values are what makes this a single request per course
        instead of one per assignment: the groups carry their assignments, each
        assignment carries its submission, and the submission carries his score.
        """
        return self._paged("/courses/%s/assignment_groups" % course_id,
                           {"include[]": ["assignments", "submission", "score_statistics"]})

    def folders(self, course_id):
        """The course's folder tree, or [] where the Files tab is off."""
        try:
            return self._paged("/courses/%s/folders" % course_id)
        except CanvasError as e:
            if e.status == 403 and not e.rate_limited:
                return []
            raise

    def modules(self, course_id):
        return self._paged("/courses/%s/modules" % course_id, {"include[]": "items"})

    def file(self, file_id):
        return self._get(self.base + "/files/%s" % file_id).json()

    def course_files(self, course_id, known=None):
        """Every file in a course, however the course is arranged.

        `/files` first. A 403 there does not mean there are no files: it means the
        instructor switched the Files tab off, which is common and is not an error. In
        that case the files still reachable are the ones linked from the modules, so we
        walk those instead and read each file object by id. Each row carries the module
        it came from, so the sync engine can file it under a folder named after the week
        rather than dropping everything at the top of the class.

        `known` is the set of Canvas file ids Vesta already holds for this course. In
        the modules path each file costs a request of its own, 35 of them for REM388,
        so a known file is not re-read: it comes back as a stub with no size, which
        `canvas_sync` treats as unchanged. That is what takes a daily check from twenty
        seconds to four. The price is that a file the professor *replaced* in place is
        not noticed, and passing `known=None` is the slow pass that notices it: "Check
        for updates". The `/files` path lists sizes for every file in one request, so
        it always notices and ignores `known` entirely.
        """
        known = set(known or ())
        try:
            files = self._paged("/courses/%s/files" % course_id)
        except CanvasError as e:
            # A throttle wears the same 403 as a hidden tab, and falling back on one
            # would report an empty course instead of admitting Canvas said wait.
            if e.rate_limited or e.status not in (403, 404):
                raise
            files = None
        if files is not None:
            return [dict(f, _module=None) for f in files]

        out = []
        for ref in files_from_modules(self.modules(course_id)):
            if ref["fileId"] in known:
                out.append({"id": ref["fileId"], "display_name": ref["title"],
                            "size": None, "_module": ref["module"], "_stub": True})
                continue
            try:
                f = self.file(ref["fileId"])
            except CanvasError as e:
                # One unreadable file (deleted, or locked until a later date) must not
                # take the rest of the course's materials down with it. A dead token or
                # a throttle is different: every call after this one will fail the same
                # way, so stop rather than return a course with most of it missing.
                if e.needs_token or e.rate_limited:
                    raise
                continue
            out.append(dict(f, _module=ref["module"]))
        return out

    def download(self, url, dest_path, max_bytes=MAX_DOWNLOAD_BYTES):
        """Fetch one file to disk, streamed, refusing anything over the ceiling.

        Streamed rather than held in memory because these are lecture decks, and this
        runs inside a web request on a small box. The size is checked twice: the
        `Content-Length` when Canvas sends one, and again as the bytes arrive, because
        a chunked response has no length to check up front.

        Canvas's file `url` is a `/files/:id/download` endpoint that wants the token,
        so the header goes on this request too. Returns the number of bytes written.
        """
        import httpx
        written = 0
        try:
            with httpx.stream("GET", url, headers=self._headers(),
                              timeout=max(self.timeout, 60), follow_redirects=True) as r:
                if r.status_code >= 400:
                    raise CanvasError("Canvas would not send that file (%s)." % r.status_code,
                                      r.status_code)
                declared = _number(r.headers.get("content-length"))
                if declared and declared > max_bytes:
                    raise CanvasError(_too_big(declared, max_bytes))
                with open(dest_path, "wb") as fh:
                    for chunk in r.iter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise CanvasError(_too_big(written, max_bytes))
                        fh.write(chunk)
        except CanvasError:
            _discard(dest_path)
            raise
        except Exception as e:
            _discard(dest_path)
            raise CanvasError("Could not download that file from Canvas: %s" % e)
        return written


def _is_throttle(response):
    """Whether this 403 is Canvas's rate limiter rather than a permission.

    Canvas sends "403 Forbidden (Rate Limit Exceeded)" as the body and carries an
    `X-Rate-Limit-Remaining` header that has reached zero. Either is enough.
    """
    if "rate limit" in ((response.text or "")[:400]).lower():
        return True
    remaining = _number(response.headers.get("x-rate-limit-remaining"))
    return remaining is not None and remaining <= 0


def _detail(response):
    """Canvas's own words about a failure, where it gave any."""
    try:
        body = response.json()
    except Exception:
        return (response.text or "")[:200]
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            msg = first.get("message") if isinstance(first, dict) else str(first)
            if msg:
                return str(msg)[:200]
        if isinstance(errors, dict):
            return str(errors)[:200]
        for key in ("message", "error", "status"):
            if body.get(key):
                return str(body[key])[:200]
    return (response.text or "")[:200]


def _too_big(size, limit):
    return ("That file is %.0f MB, over Vesta's %.0f MB limit, so it is left as a link to "
            "Canvas." % (size / 1024.0 / 1024.0, limit / 1024.0 / 1024.0))


def _discard(path):
    """A failed download leaves no half a file behind to be mistaken for a whole one."""
    import os
    try:
        os.remove(path)
    except OSError:
        pass

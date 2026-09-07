# Vesta

A real, self-hosted rebuild of the Term Board school dashboard (classes, calendar, grades, to-dos), built specifically to support actual file uploads, which the Claude-artifact version couldn't do.

**Status:** Built and tested locally. Repo: [github.com/saifabuhaltam/vesta](https://github.com/saifabuhaltam/vesta) — not deployed yet.
**Stack:** Flask + SQLite + vanilla JS frontend (no build step, no frameworks).

## What it does

- Classes with schedule, professor, notes, color
- Assignments/quizzes/exams/readings with due dates, grade weights, scores, subtasks
- A month and list calendar view, filterable by class, with .ics export
- Per-class tabs: **Files** (real uploads, drag-and-drop, auto-categorized by filename — "Lecture 7.pdf" guesses Lecture slides, "Essay Rubric.docx" guesses Rubrics, "Midterm 2025.pdf" guesses Past exams — override any guess with the inline dropdown), **Assignments**, **Notes** (auto-links to an assignment when you mention its title), **Grades** (full weighted breakdown), **Syllabus** (a topic checklist)
- **Rubric parsing** — any file categorized as a rubric gets a "Parse rubric" action that extracts its grading criteria via Claude, which can then be linked to a specific assignment. Once linked, the criteria show up on that assignment's detail view and feed into Headstart's context.
- **Headstart** — a dedicated tab listing everything open, plus a "Headstart" button on any assignment. Depending on the assignment's type it offers a draft, an essay outline, quiz prep, a study outline, or a synthesis of the class's readings (using their extracted text, plus a linked rubric's criteria if one exists). Generated work stays attached to that assignment — accept it, edit it, ask for changes, or regenerate it from scratch. Calls the Claude API server-side; see the API key setup below.
- **Lock In** — a Pomodoro-style focus timer tied to a specific assignment. Time spent in "work" phases logs back onto that assignment (visible on its detail view), so the loop of upload materials → Headstart drafts something → Lock In session to work on it → time logged is real, not just a diagram.
- Everything persists in a SQLite database on disk

## Local development

Requires Python 3.9+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Visit `http://localhost:5000`. Data is stored in `data/vesta.db` and `data/uploads/` (both gitignored). Delete the `data/` folder to start fresh.

### Headstart's API key

Headstart and rubric parsing both call the Claude API from the server, which needs your own Anthropic API key (get one at [console.anthropic.com](https://console.anthropic.com)) — this is billed per generation, separate from any Claude subscription. Set it as an environment variable named `ANTHROPIC_API_KEY`. Locally:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python app.py
```

Every other feature works without this key — Headstart and rubric parsing just return a clear error until it's set.

## Deploying to Railway

1. Code is already pushed to [github.com/saifabuhaltam/vesta](https://github.com/saifabuhaltam/vesta).
2. In Railway, create a new project from that GitHub repo. Railway auto-detects it's a Python app (via `requirements.txt` and `Procfile`) and builds it with no extra config.
3. **Add a Volume** in the Railway service settings, mounted at `/data`. This is what makes uploads and the database survive restarts and redeploys — without it, every deploy wipes your data.
4. Add an environment variable: `DATA_DIR=/data` (this tells the app where to put its database and uploaded files — see `db.py`).
5. Add `ANTHROPIC_API_KEY` as another environment variable if you want Headstart to work (see above). Everything else runs fine without it.
6. Deploy. Railway gives you a public URL immediately; add a custom domain later if you want one.

## Pushing future changes to GitHub

The `origin` remote is already set to the repo above, so after this initial push, future updates are just:

```bash
cd "projects/vesta"
git add .
git commit -m "Describe the change"
git push
```

## Known limitations / next steps

- **AI syllabus parsing isn't built yet.** The groundwork is there — PDF and DOCX text gets extracted on upload and stored (`materials.extracted_text` in the database) — but turning that into "paste or upload a syllabus and get assignments/dates/grading weights auto-filled" needs an Anthropic API key of your own (a small per-use cost) and a review step before anything gets written to your calendar, so a bad extraction can't silently create a wrong deadline.
- The dev server (`python app.py`) is for local testing only. Railway runs it through `gunicorn` (see `Procfile`), which is production-appropriate.
- Single user, no login. Anyone with the URL can see and edit everything — fine for personal use, not for sharing the link publicly. Add authentication before doing that.
- Max upload size is 25 MB per file (`app.py`, `MAX_CONTENT_LENGTH`) — raise it there if you need to.

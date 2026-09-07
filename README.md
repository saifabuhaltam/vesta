# Vesta

A real, self-hosted rebuild of the Term Board school dashboard (classes, calendar, grades, to-dos), built specifically to support actual file uploads, which the Claude-artifact version couldn't do.

**Status:** Built and tested locally. Repo: [github.com/saifabuhaltam/vesta](https://github.com/saifabuhaltam/vesta) — not deployed yet.
**Stack:** Flask + SQLite + vanilla JS frontend (no build step, no frameworks).

## What it does

- Classes with schedule, professor, notes, color
- Assignments/quizzes/exams/readings with due dates, grade weights, scores, subtasks
- A month and list calendar view, filterable by class, with .ics export
- Per-class tabs: **Files** (real uploads, drag-and-drop, categorized, with PDF/DOCX text extracted on upload for future use), **Assignments**, **Notes** (auto-links to an assignment when you mention its title), **Grades** (full weighted breakdown), **Syllabus** (a topic checklist)
- Everything persists in a SQLite database on disk

## Local development

Requires Python 3.9+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Visit `http://localhost:5000`. Data is stored in `data/vesta.db` and `data/uploads/` (both gitignored). Delete the `data/` folder to start fresh.

## Deploying to Railway

1. Code is already pushed to [github.com/saifabuhaltam/vesta](https://github.com/saifabuhaltam/vesta).
2. In Railway, create a new project from that GitHub repo. Railway auto-detects it's a Python app (via `requirements.txt` and `Procfile`) and builds it with no extra config.
3. **Add a Volume** in the Railway service settings, mounted at `/data`. This is what makes uploads and the database survive restarts and redeploys — without it, every deploy wipes your data.
4. Add an environment variable: `DATA_DIR=/data` (this tells the app where to put its database and uploaded files — see `db.py`).
5. Deploy. Railway gives you a public URL immediately; add a custom domain later if you want one.

No other environment variables are required to run the core dashboard.

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

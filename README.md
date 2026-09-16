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
PORT=5055 .venv/bin/python app.py
```

Visit `http://localhost:5055`. Data is stored in `data/vesta.db` and `data/uploads/` (both gitignored). Delete the `data/` folder to start fresh.

`PORT` defaults to 5000, but **on macOS the AirPlay Receiver in Control Center permanently occupies port 5000**, so Vesta either fails to bind or you reach the wrong service. Pass `PORT` as above, or turn AirPlay Receiver off under System Settings → General → AirDrop & Handoff. Whichever port you settle on, register its callback URL in Google Cloud if you want calendar sync — see the notes in `.env`.

### Headstart's API key

Headstart and rubric parsing both call the Claude API from the server, which needs your own Anthropic API key (get one at [console.anthropic.com](https://console.anthropic.com)) — this is billed per generation, separate from any Claude subscription. Set it as an environment variable named `ANTHROPIC_API_KEY`. Locally:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
PORT=5055 .venv/bin/python app.py
```

Easier: put it in a `.env` file next to `app.py` as `ANTHROPIC_API_KEY=sk-ant-...`. Vesta reads that at startup, and the file is gitignored. The Google Calendar credentials live there too.

Every other feature works without this key — Headstart and rubric parsing just return a clear error until it's set.

## Deploying to Railway

1. Code is already pushed to [github.com/saifabuhaltam/vesta](https://github.com/saifabuhaltam/vesta).
2. In Railway, create a new project from that GitHub repo. Railway auto-detects it's a Python app (via `requirements.txt` and `Procfile`). `nixpacks.toml` adds LibreOffice to the build so Word, PowerPoint and Excel uploads can be converted to PDF and previewed in the browser; nothing else needs configuring.
3. **Add a Volume** in the Railway service settings, mounted at `/data`. This is what makes uploads and the database survive restarts and redeploys — without it, every deploy wipes your data.
4. Add an environment variable: `DATA_DIR=/data` (this tells the app where to put its database and uploaded files — see `db.py`).
5. Add `ANTHROPIC_API_KEY` as another environment variable if you want Headstart to work (see above). Everything else runs fine without it.
6. Deploy. Railway gives you a public URL immediately; add a custom domain later if you want one.

## Environment variables

| Variable | What it does |
| --- | --- |
| `ANTHROPIC_API_KEY` | Headstart and rubric parsing. Everything else works without it. |
| `DATA_DIR` | Where the database and uploads live. `/data` on Railway, to match the volume. |
| `DATABASE_URL` | Set it and Vesta uses Postgres instead of SQLite, with accounts and RLS. |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Turn accounts on. Supabase issues tokens; Flask holds the session. |
| `SECRET_KEY` | Signs the session cookie. Required whenever accounts are on. |
| `INVITE_EMAILS` | Comma-separated allowlist. **Unset means anyone can sign up.** |
| `AI_GLOBAL_DAILY_CAP_USD` | Ceiling on AI spend across *all* accounts. The per-account cap is user-editable and is not a spend control. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google Calendar sync. |

## Tests

```bash
.venv/bin/python -m pip install pgserver "psycopg[binary]" pytest esprima
.venv/bin/python -m pytest tests/test_auth.py    # accounts, against SQLite
.venv/bin/python -m pytest tests/pg              # isolation, against a real Postgres
```

Run the two separately. `db.py` picks its database at import time, so one process
cannot host both. `tests/pg` downloads nothing and needs no installed Postgres:
`pgserver` brings its own, and runs it as a non-superuser role on purpose, because as a
superuser, row level security is bypassed and every isolation test would pass
regardless of whether the policies worked.

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
- **Accounts are on where Supabase is configured, and only there.** Run locally with no
  `SUPABASE_URL`, Vesta stays the single-user tool it began as: no login, no session.
  Deployed, every request needs a session and Postgres row level security keeps each
  account's data apart. Before sharing the URL, check `/health` reports
  `rlsEnforced: true`. If it does not, the connecting database role bypasses row level
  security and the accounts are not actually separated.
- Max upload size is 25 MB per file (`app.py`, `MAX_CONTENT_LENGTH`) — raise it there if you need to.
- **Office previews need LibreOffice.** Word, PowerPoint and Excel files are converted to PDF once, in the background, when they're uploaded, and that PDF is what the preview pane shows. On Railway this comes from `nixpacks.toml`. Locally it's optional: install LibreOffice (`brew install --cask libreoffice`) and Vesta finds it on its own, or set `SOFFICE_PATH` to the binary. Without it nothing breaks — those files just fall back to a Download button, and any that were uploaded meanwhile get converted the next time the app starts with LibreOffice available.

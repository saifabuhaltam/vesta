#!/usr/bin/env python3
"""Export the local SQLite Vesta database as SQL that can be pasted into the
Supabase SQL editor.

Read-only. It never writes to vesta.db.

Every original uuid is preserved, so anything that referenced an id before still
resolves afterwards. Ownership is resolved at run time from the email you pass,
so the generated file contains no user id and is safe to re-run against a project
where you have already signed in.

    python3 cloud/migrate/export_sqlite.py --email you@example.com > cloud/migrate/import.sql

Then paste import.sql into the Supabase SQL editor and run it.

Uploaded files are NOT included: their bytes live on this machine under
data/uploads and have to go to R2 separately. The script prints a manifest of
them to stderr so nothing is silently lost.
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "..", "..", "data", "vesta.db")

# Old material.category values that no longer exist map onto the current set.
CATEGORY_MAP = {
    "lectures": "slides",
    "lecture": "slides",
    "notes": "personal",
    "": "other",
    None: "other",
}
VALID_CATEGORIES = {
    "syllabus", "slides", "readings", "rubrics",
    "exams", "projects", "personal", "other",
}
VALID_TYPES = {
    "assignment", "homework", "quiz", "exam",
    "reading", "discussion", "project", "other",
}
VALID_STATUS = {"todo", "in_progress", "done"}


def q(v):
    """Quote a Python value as a SQL literal."""
    if v is None or v == "":
        return "null" if v is None else "''"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def qjson(v):
    if v is None:
        return "null"
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return "null"
    return "'" + json.dumps(v).replace("'", "''") + "'::jsonb"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True,
                    help="the account that should own the imported data")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        sys.exit(f"No database at {db_path}")

    # read-only connection, so a mistake here cannot damage the live app
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def rows(sql, *params):
        return conn.execute(sql, params).fetchall()

    out = []
    w = out.append

    w("-- Vesta: import of local SQLite data.")
    w(f"-- Source: {db_path}")
    w("-- Safe to run once. Re-running will fail on duplicate ids, which is the point.")
    w("")
    w("begin;")
    w("")
    w("-- Resolve the owner once. Everything below hangs off this.")
    w("do $$")
    w("declare owner_id uuid;")
    w("begin")
    w(f"  select id into owner_id from auth.users where lower(email) = lower({q(args.email)});")
    w("  if owner_id is null then")
    w(f"    raise exception 'No Vesta account for {args.email}. Sign in once first, then re-run this.';")
    w("  end if;")
    w("  perform set_config('vesta.owner', owner_id::text, true);")
    w("end $$;")
    w("")

    OWNER = "current_setting('vesta.owner')::uuid"

    # ---- semester, from the old single-row term_settings -----------------
    term = conn.execute("select * from term_settings where id=1").fetchone()
    if term and (term["name"] or term["start_date"] or term["end_date"]):
        w("-- the old single term becomes the active semester")
        w("update semesters set name=%s, start_date=%s, end_date=%s" % (
            q(term["name"] or ""),   # keep it empty if it was empty; don't invent one
            q(term["start_date"] or None),
            q(term["end_date"] or None),
        ))
        w(f"  where user_id = {OWNER} and is_active;")
        w("")

    # ---- classes ---------------------------------------------------------
    classes = rows("select * from classes order by created_at")
    if classes:
        w("-- classes")
        for c in classes:
            w("insert into classes (id, user_id, semester_id, code, name, professor, color, notes, website, grade_scale, created_at) values")
            w("  (%s, %s, (select id from semesters where user_id=%s and is_active limit 1), %s, %s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()));" % (
                q(c["id"]), OWNER, OWNER,
                q(c["code"] or ""), q(c["name"] or ""), q(c["professor"] or ""),
                q(c["color"] or "#3576D9"), q(c["notes"] or ""), q(c["website"] or ""),
                qjson(c["grade_scale"]), q(c["created_at"]),
            ))
        w("")

    # ---- schedule --------------------------------------------------------
    sched = rows("select * from schedule_entries")
    if sched:
        w("-- class meeting times")
        for s in sched:
            w("insert into schedule_entries (id, user_id, class_id, day, start_time, end_time, location) values (%s, %s, %s, %s, %s, %s, %s);" % (
                q(s["id"]), OWNER, q(s["class_id"]), s["day"] if s["day"] is not None else 0,
                q(s["start"]), q(s["end"]), q(s["location"] or ""),
            ))
        w("")

    # ---- items -----------------------------------------------------------
    items = rows("select * from items order by created_at")
    skipped = []
    if items:
        w("-- assignments, exams, readings")
        for it in items:
            typ = it["type"] if it["type"] in VALID_TYPES else "other"
            status = it["status"] if it["status"] in VALID_STATUS else "todo"
            w("insert into items (id, user_id, class_id, title, type, due_date, due_time, status, completed_at, weight, score, notes, focus_seconds, created_at) values")
            w("  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()));" % (
                q(it["id"]), OWNER, q(it["class_id"]), q(it["title"] or ""), q(typ),
                q(it["due_date"] or None), q(it["due_time"]), q(status),
                q(it["completed_at"] or None),
                it["weight"] if it["weight"] is not None else "null",
                it["score"] if it["score"] is not None else "null",
                q(it["notes"] or ""), it["focus_seconds"] or 0, q(it["created_at"]),
            ))
        w("")

    subs = rows("select * from subtasks")
    if subs:
        w("-- subtasks")
        for s in subs:
            w("insert into subtasks (id, user_id, item_id, title, done) values (%s, %s, %s, %s, %s);" % (
                q(s["id"]), OWNER, q(s["item_id"]), q(s["title"] or ""),
                "true" if s["done"] else "false",
            ))
        w("")

    # ---- events ----------------------------------------------------------
    events = rows("select * from events")
    if events:
        w("-- standalone calendar events")
        for e in events:
            w("insert into events (id, user_id, class_id, title, kind, date, start_time, end_time, all_day, location, notes, created_at) values")
            w("  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()));" % (
                q(e["id"]), OWNER, q(e["class_id"]), q(e["title"] or ""),
                q(e["kind"] or "other"), q(e["date"]), q(e["start"]), q(e["end"]),
                "true" if e["all_day"] else "false",
                q(e["location"] or ""), q(e["notes"] or ""), q(e["created_at"]),
            ))
        w("")

    # ---- note folders and notes -----------------------------------------
    folders = rows("select * from note_folders")
    if folders:
        w("-- note folders")
        for f in folders:
            w("insert into note_folders (id, user_id, class_id, name, created_at) values (%s, %s, %s, %s, coalesce(%s::timestamptz, now()));" % (
                q(f["id"]), OWNER, q(f["class_id"]), q(f["name"] or "Untitled folder"), q(f["created_at"]),
            ))
        w("")

    notes = rows("select * from notes order by created_at")
    if notes:
        w("-- notes. The old column was `text`; it is `body` now.")
        for n in notes:
            w("insert into notes (id, user_id, class_id, folder_id, linked_item_id, title, body, created_at, updated_at) values")
            w("  (%s, %s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()), coalesce(%s::timestamptz, now()));" % (
                q(n["id"]), OWNER, q(n["class_id"]), q(n["folder_id"]),
                q(n["linked_item_id"]), q(n["title"] or ""), q(n["text"] or ""),
                q(n["created_at"]), q(n["updated_at"] or n["created_at"]),
            ))
        w("")

    # ---- syllabus --------------------------------------------------------
    topics = rows("select * from syllabus_topics order by sort_order")
    if topics:
        w("-- syllabus topics")
        for t in topics:
            w("insert into syllabus_topics (id, user_id, class_id, title, done, sort_order) values (%s, %s, %s, %s, %s, %s);" % (
                q(t["id"]), OWNER, q(t["class_id"]), q(t["title"] or ""),
                "true" if t["done"] else "false", t["sort_order"] or 0,
            ))
        w("")

    # ---- headstarts ------------------------------------------------------
    hs = rows("select * from headstarts")
    if hs:
        w("-- Headstart drafts")
        for h in hs:
            status = h["status"] if h["status"] in {"draft", "generating", "ready", "error"} else "draft"
            w("insert into headstarts (id, user_id, item_id, kind, content, status, instructions, created_at) values")
            w("  (%s, %s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()));" % (
                q(h["id"]), OWNER, q(h["item_id"]), q(h["kind"]),
                q(h["content"] or ""), q(status), q(h["instructions"] or ""), q(h["created_at"]),
            ))
        w("")

    # ---- materials -> files + attachments --------------------------------
    mats = rows("select * from materials order by created_at")
    manifest = []
    if mats:
        w("-- materials become a file row (the blob) plus an attachment row (where it shows up).")
        w("-- Uploaded blobs still have to be pushed to R2; see the manifest this script printed.")
        for m in mats:
            cat = m["category"]
            if cat not in VALID_CATEGORIES:
                cat = CATEGORY_MAP.get(cat, "other")
            title = m["title"] or m["filename"] or "Untitled"
            if m["kind"] == "link" or (m["url"] and not m["stored_name"]):
                w("insert into attachments (id, user_id, url, title, category, class_id, created_at) values (%s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()));" % (
                    q(m["id"]), OWNER, q(m["url"]), q(title), q(cat), q(m["class_id"]), q(m["created_at"]),
                ))
            else:
                file_id = m["id"]
                r2_key = f"u/OWNER/{file_id}/{m['filename'] or 'file'}"
                manifest.append({
                    "material_id": m["id"],
                    "local_file": m["stored_name"],
                    "filename": m["filename"],
                    "r2_key_template": r2_key,
                    "size": m["size"],
                })
                w("insert into files (id, user_id, r2_key, filename, mimetype, size_bytes, status, extracted_text, created_at) values")
                w("  (%s, %s, %s, %s, %s, %s, 'pending', %s, coalesce(%s::timestamptz, now()));" % (
                    q(file_id), OWNER,
                    "'u/' || %s || '/%s/' || %s" % (OWNER, file_id, q(m["filename"] or "file")),
                    q(m["filename"] or ""), q(m["mimetype"] or "application/octet-stream"),
                    m["size"] or 0, q(m["extracted_text"]), q(m["created_at"]),
                ))
                # The attachment keeps the original material id, so anything in the
                # UI that held onto a material id still resolves after the move.
                w("insert into attachments (id, user_id, file_id, title, category, class_id, created_at) values (%s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()));" % (
                    q(m["id"]), OWNER, q(file_id), q(title), q(cat), q(m["class_id"]), q(m["created_at"]),
                ))
        w("")

    # ---- rubrics ---------------------------------------------------------
    rubs = rows("select * from rubrics")
    if rubs:
        w("-- rubrics")
        for r in rubs:
            w("insert into rubrics (id, user_id, file_id, item_id, criteria, total_points, created_at) values")
            w("  (%s, %s, %s, %s, coalesce(%s, '[]'::jsonb), %s, coalesce(%s::timestamptz, now()));" % (
                q(r["id"]), OWNER, q(r["material_id"]), q(r["item_id"]),
                qjson(r["criteria"]),
                r["total_points"] if r["total_points"] is not None else "null",
                q(r["created_at"]),
            ))
        w("")

    w("commit;")
    w("")
    w("-- Verify:")
    w("--   select 'classes', count(*) from classes")
    w("--   union all select 'items', count(*) from items")
    w("--   union all select 'notes', count(*) from notes")
    w("--   union all select 'files', count(*) from files;")

    print("\n".join(out))

    # ---- manifest to stderr so it does not pollute the SQL ---------------
    counts = {
        "classes": len(classes), "schedule": len(sched), "items": len(items),
        "subtasks": len(subs), "events": len(events), "note_folders": len(folders),
        "notes": len(notes), "syllabus": len(topics), "headstarts": len(hs),
        "materials": len(mats), "rubrics": len(rubs),
    }
    print("\nExported:", file=sys.stderr)
    for k, v in counts.items():
        if v:
            print(f"  {v:>4}  {k}", file=sys.stderr)
    if manifest:
        mpath = os.path.join(HERE, "upload_manifest.json")
        with open(mpath, "w") as fh:
            json.dump(manifest, fh, indent=2)
        print(f"\n  {len(manifest)} uploaded file(s) still need pushing to R2.", file=sys.stderr)
        print(f"  Manifest written to {mpath}", file=sys.stderr)
        print("  Their rows import as status='pending' and will show as still uploading", file=sys.stderr)
        print("  until the bytes are in R2 and the row is flipped to 'complete'.", file=sys.stderr)
    if skipped:
        print(f"\n  Skipped: {skipped}", file=sys.stderr)

    conn.close()


if __name__ == "__main__":
    main()

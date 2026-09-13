-- Vesta cloud schema
-- Run this once against a fresh Supabase project (SQL Editor, or `supabase db push`).
--
-- Design rules this file follows, so future migrations stay consistent:
--
--  1. EVERY user table carries `user_id uuid not null default auth.uid()`.
--     Denormalising the owner onto child rows (subtasks, schedule entries) instead of
--     joining up to the parent keeps every Row Level Security policy a single indexed
--     equality check. It is the pattern Supabase recommends and it stays fast.
--  2. IDs are uuid and are supplied by the client where useful. The existing local
--     SQLite data already uses uuid4 strings, so the migration preserves every id and
--     nothing that references an id has to be rewritten.
--  3. Deletes cascade from the owner down. Removing an auth user removes their world.
--  4. Soft delete (`deleted_at`) is only on the things the user asked to be able to
--     recover: notes and files. Everything else deletes for real.
--  5. Timestamps are timestamptz, always. Dates that mean "a calendar day with no
--     timezone" (a due date, a term start) stay `date`.

-- gen_random_uuid() has been part of core PostgreSQL since 13, so pgcrypto is
-- only needed on older servers. Supabase already has it. Tolerate its absence so
-- this file also runs against a plain Postgres for testing.
do $$
begin
  create extension if not exists "pgcrypto";
exception when others then
  raise notice 'pgcrypto unavailable (%); continuing, gen_random_uuid() is core in PG13+.', sqlerrm;
end;
$$;

-- ---------------------------------------------------------------------------
-- profiles: the app's own row per auth user
-- ---------------------------------------------------------------------------
create table if not exists profiles (
  id           uuid primary key references auth.users(id) on delete cascade,
  email        text,
  display_name text,
  avatar_url   text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- allowed_emails: signup is invite-only, so a stranger cannot create an account
-- and start consuming storage.
--
-- This is an email allowlist rather than a redeemable invite code on purpose.
-- A code has to be carried through the signup request, and the Google OAuth
-- round trip gives us nowhere reliable to put one. An email is present on every
-- new user no matter which provider they came in through, so one check covers
-- Google sign-in and email/password identically.
--
-- Add people by hand:  insert into allowed_emails (email) values ('friend@x.com');
-- ---------------------------------------------------------------------------
create table if not exists allowed_emails (
  email      text primary key,
  note       text,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- semesters: replaces the old single-row term_settings
-- ---------------------------------------------------------------------------
create table if not exists semesters (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  name       text not null default '',
  start_date date,
  end_date   date,
  is_active  boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists semesters_user_idx on semesters(user_id);
-- at most one active semester per user
create unique index if not exists semesters_one_active_idx
  on semesters(user_id) where is_active;

-- ---------------------------------------------------------------------------
-- classes
-- ---------------------------------------------------------------------------
create table if not exists classes (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  semester_id uuid references semesters(id) on delete set null,
  code        text not null default '',
  name        text not null default '',
  professor   text not null default '',
  color       text not null default '#3576D9',
  notes       text not null default '',
  website     text not null default '',
  grade_scale jsonb,
  sort_order  integer not null default 0,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists classes_user_idx     on classes(user_id);
create index if not exists classes_semester_idx on classes(semester_id);

create table if not exists schedule_entries (
  id       uuid primary key default gen_random_uuid(),
  user_id  uuid not null default auth.uid() references auth.users(id) on delete cascade,
  class_id uuid not null references classes(id) on delete cascade,
  day      smallint not null check (day between 0 and 6),   -- 0 = Sunday
  start_time text,
  end_time   text,
  location   text not null default ''
);
create index if not exists schedule_user_idx  on schedule_entries(user_id);
create index if not exists schedule_class_idx on schedule_entries(class_id);

-- ---------------------------------------------------------------------------
-- items: assignments, exams, quizzes, readings
-- ---------------------------------------------------------------------------
create table if not exists items (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null default auth.uid() references auth.users(id) on delete cascade,
  class_id      uuid references classes(id) on delete cascade,
  title         text not null default '',
  type          text not null default 'assignment'
                check (type in ('assignment','homework','quiz','exam','reading','discussion','project','other')),
  due_date      date,
  due_time      text,
  status        text not null default 'todo'
                check (status in ('todo','in_progress','done')),
  completed_at  date,
  weight        numeric,
  score         numeric,
  notes         text not null default '',
  focus_seconds integer not null default 0,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists items_user_idx  on items(user_id);
create index if not exists items_class_idx on items(class_id);
create index if not exists items_due_idx   on items(user_id, due_date);

create table if not exists subtasks (
  id      uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  item_id uuid not null references items(id) on delete cascade,
  title   text not null default '',
  done    boolean not null default false,
  sort_order integer not null default 0
);
create index if not exists subtasks_user_idx on subtasks(user_id);
create index if not exists subtasks_item_idx on subtasks(item_id);

-- ---------------------------------------------------------------------------
-- calendar events that are not tied to an item (office hours, study groups)
-- ---------------------------------------------------------------------------
create table if not exists events (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  class_id   uuid references classes(id) on delete cascade,
  title      text not null default '',
  kind       text not null default 'other',
  date       date not null,
  start_time text,
  end_time   text,
  all_day    boolean not null default false,
  location   text not null default '',
  notes      text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists events_user_idx on events(user_id);
create index if not exists events_date_idx on events(user_id, date);

-- ---------------------------------------------------------------------------
-- notes, their folders, their version history
-- ---------------------------------------------------------------------------
create table if not exists note_folders (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  class_id   uuid references classes(id) on delete cascade,
  name       text not null default 'Untitled folder',
  created_at timestamptz not null default now()
);
create index if not exists note_folders_user_idx  on note_folders(user_id);
create index if not exists note_folders_class_idx on note_folders(class_id);

create table if not exists notes (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null default auth.uid() references auth.users(id) on delete cascade,
  class_id       uuid references classes(id) on delete cascade,
  folder_id      uuid references note_folders(id) on delete set null,
  linked_item_id uuid references items(id) on delete set null,
  title          text not null default '',
  body           text not null default '',      -- HTML from the rich text editor
  -- Monotonic counter the client bumps on every save. Lets an offline device
  -- detect that the server moved on while it was away, instead of blindly
  -- overwriting a newer copy written from another device.
  revision       bigint not null default 1,
  deleted_at     timestamptz,                   -- 30-day Trash
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index if not exists notes_user_idx    on notes(user_id);
create index if not exists notes_class_idx   on notes(class_id);
create index if not exists notes_folder_idx  on notes(folder_id);
create index if not exists notes_live_idx    on notes(user_id) where deleted_at is null;
create index if not exists notes_trash_idx   on notes(user_id, deleted_at) where deleted_at is not null;

-- One row per saved version. Written by a trigger, not by the client, so history
-- cannot be forged or skipped.
create table if not exists note_versions (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  note_id    uuid not null references notes(id) on delete cascade,
  revision   bigint not null,
  title      text not null default '',
  body       text not null default '',
  created_at timestamptz not null default now()
);
create index if not exists note_versions_note_idx on note_versions(note_id, created_at desc);
create index if not exists note_versions_user_idx on note_versions(user_id);

-- ---------------------------------------------------------------------------
-- syllabus topics
-- ---------------------------------------------------------------------------
create table if not exists syllabus_topics (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  class_id   uuid not null references classes(id) on delete cascade,
  title      text not null default '',
  done       boolean not null default false,
  sort_order integer not null default 0
);
create index if not exists syllabus_user_idx  on syllabus_topics(user_id);
create index if not exists syllabus_class_idx on syllabus_topics(class_id);

-- ---------------------------------------------------------------------------
-- files: one row per distinct uploaded blob in R2.
--
-- The blob is stored ONCE. Where it shows up in the app is decided entirely by
-- rows in `attachments`, so the same PDF linked to a class, an assignment and a
-- note costs one object in R2 and one row here.
--
-- `status` is the upload state machine. A row is created as 'pending' before the
-- browser starts pushing bytes to R2, and is only flipped to 'complete' once the
-- upload is confirmed. Anything left 'pending' is an abandoned upload and is safe
-- to sweep.
-- ---------------------------------------------------------------------------
create table if not exists files (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null default auth.uid() references auth.users(id) on delete cascade,
  r2_key         text not null,
  sha256         text,                 -- content hash, used to dedupe within one user
  filename       text not null default '',
  mimetype       text not null default 'application/octet-stream',
  size_bytes     bigint not null default 0,
  status         text not null default 'pending'
                 check (status in ('pending','uploading','complete','failed')),
  upload_id      text,                 -- R2 multipart upload id, for resumable large files
  bytes_received bigint not null default 0,
  extracted_text text,                 -- filled in for PDF/DOCX so Headstart can read them
  -- The UI only ever asks whether text was extracted, never for the text itself.
  -- Exposing it as a column keeps the extracted body (which can be 200 KB) out of
  -- the payload every time the file list loads.
  has_text boolean generated always as (extracted_text is not null) stored,
  deleted_at     timestamptz,          -- 30-day Trash
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create unique index if not exists files_r2_key_idx on files(r2_key);
-- the dedupe key: same user, same bytes, one row
create unique index if not exists files_user_sha_idx
  on files(user_id, sha256) where sha256 is not null and deleted_at is null;
create index if not exists files_user_idx   on files(user_id);
create index if not exists files_status_idx on files(user_id, status);

-- ---------------------------------------------------------------------------
-- attachments: "this resource appears in this place".
--
-- Replaces the old `materials` table. Exactly one target column is set, which
-- Postgres enforces below, so deletes cascade correctly and there is no
-- polymorphic target_id that the database cannot check.
--
-- An attachment is either a stored file (file_id set) or an external bookmark
-- (url set), never both.
-- ---------------------------------------------------------------------------
create table if not exists attachments (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  file_id     uuid references files(id) on delete cascade,
  url         text,
  title       text not null default '',
  category    text not null default 'other'
              check (category in ('syllabus','slides','readings','rubrics','exams','projects','personal','other')),
  class_id     uuid references classes(id) on delete cascade,
  item_id      uuid references items(id) on delete cascade,
  note_id      uuid references notes(id) on delete cascade,
  headstart_id uuid,   -- FK added after headstarts is created, below
  starred     boolean not null default false,
  created_at  timestamptz not null default now(),
  constraint attachments_is_file_or_link check (
    (file_id is not null and url is null) or
    (file_id is null and url is not null)
  ),
  constraint attachments_exactly_one_target check (
    (case when class_id     is not null then 1 else 0 end) +
    (case when item_id      is not null then 1 else 0 end) +
    (case when note_id      is not null then 1 else 0 end) +
    (case when headstart_id is not null then 1 else 0 end) = 1
  )
);
create index if not exists attachments_user_idx  on attachments(user_id);
create index if not exists attachments_file_idx  on attachments(file_id);
create index if not exists attachments_class_idx on attachments(class_id);
create index if not exists attachments_item_idx  on attachments(item_id);
create index if not exists attachments_note_idx  on attachments(note_id);

-- ---------------------------------------------------------------------------
-- Headstart: AI-generated drafts, outlines and study prep, per item
-- ---------------------------------------------------------------------------
create table if not exists headstarts (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null default auth.uid() references auth.users(id) on delete cascade,
  item_id      uuid not null references items(id) on delete cascade,
  kind         text not null
               check (kind in ('draft','essay_outline','quiz_prep','study_outline','synthesis','explain')),
  content      text not null default '',
  status       text not null default 'draft'
               check (status in ('draft','generating','ready','error')),
  instructions text not null default '',
  model        text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (item_id, kind)
);
create index if not exists headstarts_user_idx on headstarts(user_id);
create index if not exists headstarts_item_idx on headstarts(item_id);

alter table attachments
  drop constraint if exists attachments_headstart_id_fkey;
alter table attachments
  add constraint attachments_headstart_id_fkey
  foreign key (headstart_id) references headstarts(id) on delete cascade;

-- ---------------------------------------------------------------------------
-- rubrics, parsed out of an uploaded rubric file
-- ---------------------------------------------------------------------------
create table if not exists rubrics (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null default auth.uid() references auth.users(id) on delete cascade,
  file_id      uuid references files(id) on delete cascade,
  item_id      uuid references items(id) on delete set null,
  criteria     jsonb not null default '[]'::jsonb,
  total_points numeric,
  created_at   timestamptz not null default now()
);
create index if not exists rubrics_user_idx on rubrics(user_id);
create index if not exists rubrics_item_idx on rubrics(item_id);
create unique index if not exists rubrics_file_idx on rubrics(file_id) where file_id is not null;

-- ---------------------------------------------------------------------------
-- focus sessions: real measured study time, not wall clock since Start.
-- The local app already counts only ticked seconds; this is where they land so
-- the history survives a device change.
-- ---------------------------------------------------------------------------
create table if not exists focus_sessions (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  item_id    uuid references items(id) on delete set null,
  class_id   uuid references classes(id) on delete set null,
  seconds    integer not null default 0,
  started_at timestamptz not null default now(),
  ended_at   timestamptz,
  day        date not null default (now() at time zone 'utc')::date
);
create index if not exists focus_user_day_idx on focus_sessions(user_id, day);
create index if not exists focus_item_idx     on focus_sessions(item_id);

-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------------
create or replace function touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

do $$
declare t text;
begin
  foreach t in array array[
    'profiles','semesters','classes','items','events','notes','files','headstarts'
  ] loop
    execute format('drop trigger if exists %I_touch on %I', t, t);
    execute format(
      'create trigger %I_touch before update on %I
       for each row execute function touch_updated_at()', t, t);
  end loop;
end;
$$;

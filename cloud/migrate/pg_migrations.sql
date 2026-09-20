-- Vesta: Postgres migrations, applied in order at boot by db.run_pg_migrations().
--
-- pg_schema.sql only ever runs against an empty database: ensure_pg_schema() checks
-- for the `classes` table and returns if it is already there. So once the app is
-- deployed, every schema change has to arrive through this file instead.
--
-- Each block starts with `-- migration: <id>`. An id that already appears in
-- schema_migrations is skipped, so this file is append-only: never edit a block that
-- has shipped, add a new one.
--
-- Two things to remember when writing one:
--
--  * Every table carries user_id and forced Row Level Security. The migration runs as
--    the database owner, and FORCE means the owner is subject to the policies too,
--    which are granted `to authenticated`. So a plain UPDATE here matches zero rows
--    and fails silently. Any block that touches data has to lift FORCE, do its work,
--    and put it back, all inside the one transaction the runner wraps it in.
--  * A new table needs its user_id column, its index, apply_owner_rls(), and an
--    explicit grant. The blanket grant in pg_schema.sql only covered the tables that
--    existed when it ran.


-- migration: 001_semesters
-- Terms become first-class. Before this there was one `term_settings` row pinned to
-- id = 1 and nothing referenced it, so the app had a single term forever.

create table if not exists semesters (
  "id"          text primary key,
  "name"        text,
  "start_date"  text,
  "end_date"    text,
  "status"      text default 'active',
  "archived_at" text,
  "created_at"  text,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade
);
create index if not exists semesters_user_idx on semesters(user_id);

alter table classes          add column if not exists semester_id text references semesters("id") on delete set null;
alter table items            add column if not exists semester_id text references semesters("id") on delete set null;
alter table events           add column if not exists semester_id text references semesters("id") on delete set null;
alter table notes            add column if not exists semester_id text references semesters("id") on delete set null;
alter table materials        add column if not exists semester_id text references semesters("id") on delete set null;
alter table flashcard_decks  add column if not exists semester_id text references semesters("id") on delete set null;
alter table quizzes          add column if not exists semester_id text references semesters("id") on delete set null;

create index if not exists classes_by_semester         on classes(semester_id);
create index if not exists items_by_semester           on items(semester_id);
create index if not exists events_by_semester          on events(semester_id);
create index if not exists notes_by_semester           on notes(semester_id);
create index if not exists materials_by_semester       on materials(semester_id);
create index if not exists flashcard_decks_by_semester on flashcard_decks(semester_id);
create index if not exists quizzes_by_semester         on quizzes(semester_id);

-- Lift FORCE so the owner can see every user's rows for the backfill. Restored at the
-- end of this same block, and the runner wraps the block in a transaction, so a
-- failure anywhere puts it back.
--
-- `semesters` belongs in this list even though the block just created it. On a
-- database built before this migration the new table has no policies yet, so the
-- inserts below would go through either way; but on one built from the current
-- pg_schema.sql the table arrives with forced RLS already on it, and the first
-- insert is then refused outright. Lifting it here covers both.
do $$ declare t text; begin
  foreach t in array array['semesters',
                           'term_settings','classes','items','events','notes',
                           'materials','flashcard_decks','quizzes'] loop
    execute format('alter table %I no force row level security', t);
  end loop;
end $$;

-- One semester per user, named from whatever they had typed into term settings.
insert into semesters (id, user_id, name, start_date, end_date, status, created_at)
select gen_random_uuid()::text, t.user_id,
       coalesce(nullif(btrim(t.name), ''), 'Current term'),
       coalesce(t.start_date, ''), coalesce(t.end_date, ''), 'active',
       to_char(now() at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS.US')
from term_settings t
where not exists (select 1 from semesters s where s.user_id = t.user_id);

-- And for anyone holding classes without a term_settings row to name them after.
insert into semesters (id, user_id, name, start_date, end_date, status, created_at)
select gen_random_uuid()::text, u.user_id, 'Current term', '', '', 'active',
       to_char(now() at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS.US')
from (select distinct user_id from classes) u
where not exists (select 1 from semesters s where s.user_id = u.user_id);

update classes c set semester_id = (
  select s.id from semesters s
   where s.user_id = c.user_id and s.status = 'active'
   order by s.created_at limit 1)
where c.semester_id is null;

-- A row with a class belongs wherever that class belongs; one without falls back to
-- the term that was current when it was made.
do $$ declare t text; begin
  foreach t in array array['items','events','notes','materials','flashcard_decks','quizzes'] loop
    execute format(
      'update %I x set semester_id = c.semester_id from classes c
        where c.id = x.class_id and x.semester_id is null', t);
    execute format(
      'update %I x set semester_id = (
         select s.id from semesters s
          where s.user_id = x.user_id and s.status = ''active''
          order by s.created_at limit 1)
        where x.semester_id is null', t);
  end loop;
end $$;

do $$ declare t text; begin
  foreach t in array array['semesters',
                           'term_settings','classes','items','events','notes',
                           'materials','flashcard_decks','quizzes'] loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
  end loop;
end $$;

select apply_owner_rls('semesters');
grant select, insert, update, delete on semesters to authenticated;
revoke all on semesters from anon;


-- migration: 002_calendar_feeds
-- Which Google calendars the student wants Vesta to read. Before this, Vesta only ever
-- looked at a calendar it had created for itself, so connecting an account appeared to
-- do nothing: none of the student's real calendars were ever read.
--
-- The sync token belongs here rather than on calendar_accounts because Google issues
-- one per calendar. A single shared token would send every calendar after the first
-- through a full pass on every sync.
--
-- No backfill, so no FORCE juggling is needed: the table starts empty and fills the
-- first time the student opens the calendar picker.

create table if not exists calendar_feeds (
  "id"          text primary key,
  "account_id"  text not null references calendar_accounts("id") on delete cascade,
  "calendar_id" text not null,
  "name"        text,
  "colour"      text,
  "writable"    integer default 0,
  "is_vesta"    integer default 0,
  "enabled"     integer default 0,
  "sync_token"  text,
  "last_sync"   text,
  "last_error"  text,
  "created_at"  text,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  unique ("account_id", "calendar_id")
);
create index if not exists calendar_feeds_user_idx on calendar_feeds(user_id);

-- Events mirrored from a chosen calendar say which one, so that unticking a calendar
-- can withdraw exactly its events and leave everything else alone.
alter table events add column if not exists feed_id text references calendar_feeds("id") on delete cascade;
create index if not exists events_by_feed on events(feed_id);
create index if not exists calendar_feeds_by_account on calendar_feeds("account_id");

select apply_owner_rls('calendar_feeds');
grant select, insert, update, delete on calendar_feeds to authenticated;
revoke all on calendar_feeds from anon;


-- migration: 003_file_folders
-- Files get a real folder tree, and Office uploads get a converted PDF to preview.
--
-- This block exists because the SQLite path and the Postgres path diverge here and it
-- was missed: `db.migrate_file_folders` adds these tables and columns at every local
-- start, but `init_db()` returns early when DATABASE_URL is set, so a deployed database
-- never sees it. Without this migration the deployed app raises
-- `relation "file_folders" does not exist` on the first /api/state, which is every page
-- load. Any future SQLite ALTER needs a partner block here on the same day.
--
-- No backfill of existing files. `folder_id_for_kind` creates a class's folders the
-- first time something is uploaded into one, so a library that predates this fills in
-- as it is used rather than being rearranged underneath the student.

create table if not exists file_folders (
  "id"         text primary key,
  "class_id"   text not null references classes("id") on delete cascade,
  "parent_id"  text references file_folders("id") on delete cascade,
  "name"       text,
  "kind"       text default 'custom',
  "sort_order" integer default 0,
  "created_at" text,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade
);
create index if not exists file_folders_user_idx on file_folders(user_id);
create index if not exists file_folders_by_class on file_folders("class_id");

-- Where a file sits, and the state of its converted preview. `preview_status` is one
-- of null, 'pending', 'ready' or 'failed'; 'ready' means preview_name names a PDF on
-- the volume.
alter table materials add column if not exists folder_id text references file_folders("id") on delete set null;
alter table materials add column if not exists preview_name text;
alter table materials add column if not exists preview_status text;
create index if not exists materials_by_folder on materials(folder_id);

select apply_owner_rls('file_folders');
grant select, insert, update, delete on file_folders to authenticated;
revoke all on file_folders from anon;


-- migration: 004_threads
-- Headstart gains continuing conversations: a thread per topic inside a class, its
-- sources pinned once and its history kept.
--
-- The partner block migration 003 asked for. `db.init_db()` creates these tables at
-- every local start and adds the two ai_usage columns by ALTER, but it returns early
-- when DATABASE_URL is set, so a deployed database would never see any of it and the
-- first /api/threads would raise `relation "threads" does not exist`.
--
-- No backfill. Existing one-shot Headstart runs stay where they are in `headstarts`;
-- turning them into single-message threads is a separate decision, not something to
-- do to a live database in passing.

create table if not exists threads (
  "id"          text primary key,
  "semester_id" text references semesters("id") on delete set null,
  "class_id"    text references classes("id") on delete cascade,
  "item_id"     text references items("id") on delete set null,
  "title"       text,
  "archived"    integer default 0,
  "created_at"  text,
  "updated_at"  text,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade
);
create index if not exists threads_user_idx on threads(user_id);
create index if not exists threads_by_class on threads("class_id");
create index if not exists threads_by_semester on threads("semester_id");

create table if not exists thread_messages (
  "id"                 text primary key,
  "thread_id"          text not null references threads("id") on delete cascade,
  "role"               text,
  "content"            text,
  "tool"               text,
  "input_tokens"       integer default 0,
  "output_tokens"      integer default 0,
  "cache_read_tokens"  integer default 0,
  "cache_write_tokens" integer default 0,
  "created_at"         text,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade
);
create index if not exists thread_messages_user_idx on thread_messages(user_id);
-- every turn is fetched by thread, in order, on every open
create index if not exists thread_messages_by_thread on thread_messages("thread_id", "created_at");

create table if not exists thread_sources (
  "id"          text primary key,
  "thread_id"   text not null references threads("id") on delete cascade,
  "material_id" text references materials("id") on delete cascade,
  "note_id"     text references notes("id") on delete cascade,
  "folder_id"   text references note_folders("id") on delete cascade,
  "syllabus_id" text references syllabus_topics("id") on delete cascade,
  "created_at"  text,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade
);
create index if not exists thread_sources_user_idx on thread_sources(user_id);
create index if not exists thread_sources_by_thread on thread_sources("thread_id");

-- Cached prompt tokens are billed at different rates from ordinary input, so they are
-- counted apart. Without them a caching regression is invisible: the requests still
-- succeed and only the bill moves.
alter table ai_usage add column if not exists cache_read_tokens integer default 0;
alter table ai_usage add column if not exists cache_write_tokens integer default 0;

select apply_owner_rls('threads');
select apply_owner_rls('thread_messages');
select apply_owner_rls('thread_sources');

grant select, insert, update, delete on threads to authenticated;
grant select, insert, update, delete on thread_messages to authenticated;
grant select, insert, update, delete on thread_sources to authenticated;
revoke all on threads from anon;
revoke all on thread_messages from anon;
revoke all on thread_sources from anon;


-- migration: 005_humanizer
-- Study gains a Humanizer: paste, pick or upload a piece of writing, get back a rewrite
-- with the AI habits it removed marked on the original. Each pass is kept so it can be
-- reopened. Nothing existing changes; the voice sample lives in app_settings, which is
-- already per account.

create table if not exists humanizer_runs (
  "id"            text primary key,
  "title"         text,
  "source_kind"   text,
  "source_id"     text,
  "source_label"  text,
  "original"      text,
  "final"         text,
  "tells"         text,
  "still_off"     text,
  "questions"     text,
  "used_voice"    integer default 0,
  "model"         text,
  "input_tokens"  integer default 0,
  "output_tokens" integer default 0,
  "created_at"    text,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade
);
create index if not exists humanizer_runs_user_idx on humanizer_runs(user_id);
create index if not exists humanizer_runs_by_created on humanizer_runs("created_at");

select apply_owner_rls('humanizer_runs');

grant select, insert, update, delete on humanizer_runs to authenticated;
revoke all on humanizer_runs from anon;


-- migration: 006_deck_item
-- A flashcard deck made for one assignment.
--
-- Quizzes have carried item_id since they were built, so a practice test could be
-- tied to an assignment; decks never did. The assignment card reads both now, and
-- without this column a deck built from an assignment has nothing to be found by.
--
-- The partner block for the SQLite ALTER in db.init_db(). Adding a nullable column
-- needs no backfill and no policy change: flashcard_decks already carries user_id
-- and forced row level security from the base schema.

alter table flashcard_decks add column if not exists "item_id" text
  references items(id) on delete set null;

create index if not exists flashcard_decks_by_item on flashcard_decks("item_id");


-- migration: 007_card_learn
-- How far the Learn mode has got with each card.
--
-- 0 not started, 1 recognised from four choices, 2 typed correctly. Deliberately
-- separate from ease/interval_days/repetitions: those schedule a review over days,
-- this is progress through a set that should survive closing the tab and follow the
-- account to another device.
--
-- The partner block for the SQLite ALTER in db.init_db(). A nullable column with a
-- default needs no backfill, and flashcards already carries user_id and forced row
-- level security from the base schema.

alter table flashcards add column if not exists "learn_level" integer default 0;


-- migration: 008_headstart_threads
-- Which chat a saved Headstart became.
--
-- Generated work grew two homes: the `headstarts` table, written by the one-shot
-- tools, and threads, written by the conversations that replaced them. Saif chose one
-- home. `migrate_headstarts_to_threads()` in db.py rewrites every saved result as a
-- chat with the tool's output as its first message, and records here which chat it
-- became, so it can run again harmlessly and an old link still finds its content.
--
-- The rows themselves are kept. Nothing in this app deletes a student's work to tidy
-- a schema.

alter table headstarts add column if not exists "thread_id" text
  references threads(id) on delete set null;

create index if not exists headstarts_by_thread on headstarts("thread_id");


-- migration: 009_calendar_channels
-- Google push channels, one per watched calendar.
--
-- Until now a change made in Google waited up to three minutes for Vesta's timer, and
-- waited indefinitely while Vesta was closed. A watch channel makes Google call us the
-- moment something moves.
--
-- Both ids are stored because stopping a channel needs the pair: a channel with only
-- its id recorded cannot be closed and keeps calling. `expiration` is what renewal
-- reads -- channels last about a week, Google never renews them, and a missed renewal
-- looks exactly like the lag this removes, which is why /health reports it.

create table if not exists calendar_channels (
  "id"           text primary key,
  "account_id"   text not null references calendar_accounts(id) on delete cascade,
  "calendar_id"  text not null,
  "channel_id"   text not null,
  "resource_id"  text,
  "expiration"   text,
  "created_at"   text,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  unique (account_id, calendar_id)
);
create index if not exists calendar_channels_user_idx on calendar_channels(user_id);
create index if not exists calendar_channels_by_channel on calendar_channels("channel_id");

select apply_owner_rls('calendar_channels');

grant select, insert, update, delete on calendar_channels to authenticated;
revoke all on calendar_channels from anon;

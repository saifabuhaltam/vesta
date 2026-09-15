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

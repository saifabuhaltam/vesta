-- Vesta triggers, retention and housekeeping.

-- ---------------------------------------------------------------------------
-- Signup gate + profile creation.
--
-- Runs on every new auth user regardless of provider, so it covers Google
-- sign-in and email/password with one check. Raising here aborts the signup
-- transaction, so a rejected email never becomes an account at all.
-- ---------------------------------------------------------------------------
create or replace function handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.email is null
     or not exists (
       select 1 from allowed_emails
       where lower(allowed_emails.email) = lower(new.email)
     )
  then
    raise exception 'This email has not been invited to Vesta.'
      using errcode = '42501';
  end if;

  insert into profiles (id, email, display_name, avatar_url)
  values (
    new.id,
    new.email,
    coalesce(
      new.raw_user_meta_data ->> 'full_name',
      new.raw_user_meta_data ->> 'name',
      split_part(new.email, '@', 1)
    ),
    new.raw_user_meta_data ->> 'avatar_url'
  )
  on conflict (id) do nothing;

  -- Give every new account one semester so the app has somewhere to put classes.
  insert into semesters (user_id, name, is_active)
  values (new.id, 'Current term', true);

  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();

-- ---------------------------------------------------------------------------
-- Note version history.
--
-- Autosave fires roughly once a second while typing, so snapshotting on every
-- write would put thousands of near-identical rows in a 500 MB database. This
-- keeps at most one version per note per 5 minutes of editing, and prunes each
-- note to its 50 most recent versions. That is enough to walk back a bad edit
-- without the history becoming the largest thing in the database.
-- ---------------------------------------------------------------------------
create or replace function snapshot_note_version()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  last_version_at timestamptz;
begin
  -- nothing to snapshot if the text did not actually change
  if new.body is not distinct from old.body
     and new.title is not distinct from old.title then
    return new;
  end if;

  select max(created_at) into last_version_at
  from note_versions where note_id = old.id;

  if last_version_at is null or last_version_at < now() - interval '5 minutes' then
    insert into note_versions (user_id, note_id, revision, title, body)
    values (old.user_id, old.id, old.revision, old.title, old.body);

    delete from note_versions
    where note_id = old.id
      and id not in (
        select id from note_versions
        where note_id = old.id
        order by created_at desc
        limit 50
      );
  end if;

  return new;
end;
$$;

drop trigger if exists notes_snapshot on notes;
create trigger notes_snapshot
  before update on notes
  for each row execute function snapshot_note_version();

-- ---------------------------------------------------------------------------
-- R2 cleanup queue.
--
-- Deleting a file row would throw away the r2_key with it, and the object would
-- sit in the bucket forever costing storage. So the key is copied here first and
-- the Cloudflare Worker's scheduled job drains the queue and deletes the objects.
-- ---------------------------------------------------------------------------
create table if not exists r2_deletion_queue (
  id           uuid primary key default gen_random_uuid(),
  r2_key       text not null,
  queued_at    timestamptz not null default now(),
  attempts     integer not null default 0,
  last_error   text,
  deleted_at   timestamptz
);
create index if not exists r2_queue_pending_idx
  on r2_deletion_queue(queued_at) where deleted_at is null;

alter table r2_deletion_queue enable row level security;
alter table r2_deletion_queue force row level security;
-- No policies, so RLS denies every client. The revoke is belt and braces: this
-- table is created after 002 ran, so Supabase's default privileges would
-- otherwise have granted it to anon and authenticated.
revoke all on r2_deletion_queue from anon, authenticated;
-- 002's blanket grant ran before this table existed, so the Worker needs its own.
grant select, insert, update, delete on r2_deletion_queue to service_role;

create or replace function queue_r2_delete()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if old.r2_key is not null and old.r2_key <> '' then
    insert into r2_deletion_queue (r2_key) values (old.r2_key);
  end if;
  return old;
end;
$$;

drop trigger if exists files_queue_r2_delete on files;
create trigger files_queue_r2_delete
  before delete on files
  for each row execute function queue_r2_delete();

-- ---------------------------------------------------------------------------
-- Retention: empty the 30-day Trash, and sweep uploads that never finished.
--
-- Called by pg_cron below. Safe to run by hand at any time.
-- ---------------------------------------------------------------------------
create or replace function run_vesta_retention()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  -- 30-day Trash for notes
  delete from notes
  where deleted_at is not null
    and deleted_at < now() - interval '30 days';

  -- 30-day Trash for files. The delete trigger queues each r2_key for the Worker.
  delete from files
  where deleted_at is not null
    and deleted_at < now() - interval '30 days';

  -- An upload that never reported completion within a day is abandoned. Removing
  -- the row also queues whatever partial object exists in R2 for deletion.
  delete from files
  where status in ('pending', 'uploading')
    and created_at < now() - interval '1 day';

  -- Versions of notes that no longer exist cannot happen (FK cascades), but
  -- history for a note that has been in the trash the whole time is dead weight.
  delete from note_versions v
  using notes n
  where v.note_id = n.id
    and n.deleted_at is not null
    and n.deleted_at < now() - interval '7 days';
end;
$$;

-- ---------------------------------------------------------------------------
-- Schedule it. pg_cron is available on Supabase including the free tier, but it
-- has to be enabled once. If this block reports a notice instead of scheduling,
-- turn on `pg_cron` under Database -> Extensions and re-run the file.
-- ---------------------------------------------------------------------------
do $$
begin
  create extension if not exists pg_cron;

  perform cron.unschedule('vesta-retention')
  where exists (select 1 from cron.job where jobname = 'vesta-retention');

  perform cron.schedule(
    'vesta-retention',
    '17 4 * * *',                  -- 04:17 UTC daily, off-peak
    $job$ select run_vesta_retention(); $job$
  );
exception when others then
  raise notice 'pg_cron not scheduled (%). Enable the pg_cron extension and re-run 003_functions.sql.', sqlerrm;
end;
$$;

-- Vesta Row Level Security
--
-- This file IS the security model. The browser talks to Postgres directly through
-- PostgREST using the anon key, which is public by design and safe to ship in the
-- page. Nothing stops a user from crafting their own query. What stops them from
-- reading someone else's classes is the policies below, and nothing else.
--
-- The rules:
--   * RLS is ON for every table. A table with RLS on and no policy denies everyone.
--   * Ownership is always `user_id = auth.uid()`. auth.uid() reads the caller's JWT,
--     so it cannot be spoofed from the client.
--   * Every policy pair uses USING for the rows you may see or change, and WITH CHECK
--     for the rows you may leave behind. Omitting WITH CHECK on an update would let a
--     user hand their row to someone else, or claim someone else's.
--   * The service role key bypasses all of this. It lives only in the Cloudflare
--     Worker's secrets and must never reach the browser.

-- ---------------------------------------------------------------------------
-- helper: apply the standard owner-only policy set to a table
-- ---------------------------------------------------------------------------
create or replace function apply_owner_rls(tbl text)
returns void
language plpgsql
as $$
begin
  execute format('alter table %I enable row level security', tbl);
  execute format('alter table %I force row level security', tbl);

  execute format('drop policy if exists %I on %I', tbl || '_select_own', tbl);
  execute format('drop policy if exists %I on %I', tbl || '_insert_own', tbl);
  execute format('drop policy if exists %I on %I', tbl || '_update_own', tbl);
  execute format('drop policy if exists %I on %I', tbl || '_delete_own', tbl);

  execute format(
    'create policy %I on %I for select to authenticated using (user_id = auth.uid())',
    tbl || '_select_own', tbl);

  execute format(
    'create policy %I on %I for insert to authenticated with check (user_id = auth.uid())',
    tbl || '_insert_own', tbl);

  -- USING decides which rows may be updated, WITH CHECK decides what they may
  -- become. Both are required: without WITH CHECK a user could reassign user_id.
  execute format(
    'create policy %I on %I for update to authenticated
       using (user_id = auth.uid()) with check (user_id = auth.uid())',
    tbl || '_update_own', tbl);

  execute format(
    'create policy %I on %I for delete to authenticated using (user_id = auth.uid())',
    tbl || '_delete_own', tbl);
end;
$$;

do $$
declare t text;
begin
  foreach t in array array[
    'semesters',
    'classes',
    'schedule_entries',
    'items',
    'subtasks',
    'events',
    'note_folders',
    'notes',
    'syllabus_topics',
    'files',
    'attachments',
    'headstarts',
    'rubrics',
    'focus_sessions'
  ] loop
    perform apply_owner_rls(t);
  end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- profiles: keyed by the auth user id itself, not a user_id column
-- ---------------------------------------------------------------------------
alter table profiles enable row level security;
alter table profiles force row level security;

drop policy if exists profiles_select_own on profiles;
drop policy if exists profiles_update_own on profiles;
drop policy if exists profiles_insert_own on profiles;

create policy profiles_select_own on profiles
  for select to authenticated using (id = auth.uid());
create policy profiles_insert_own on profiles
  for insert to authenticated with check (id = auth.uid());
create policy profiles_update_own on profiles
  for update to authenticated using (id = auth.uid()) with check (id = auth.uid());
-- deliberately no delete policy: a profile goes away with its auth user, not on demand

-- ---------------------------------------------------------------------------
-- note_versions: readable and deletable by the owner, but never written by the
-- client. Rows appear only via the trigger in 003, which runs as definer.
-- ---------------------------------------------------------------------------
alter table note_versions enable row level security;
alter table note_versions force row level security;

drop policy if exists note_versions_select_own on note_versions;
drop policy if exists note_versions_delete_own on note_versions;

create policy note_versions_select_own on note_versions
  for select to authenticated using (user_id = auth.uid());
create policy note_versions_delete_own on note_versions
  for delete to authenticated using (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- allowed_emails: RLS on, zero policies. That denies every client outright,
-- including reads. Only the signup trigger (security definer) and the service
-- role can see it, so nobody can enumerate who is allowed in.
-- ---------------------------------------------------------------------------
alter table allowed_emails enable row level security;
alter table allowed_emails force row level security;

-- ---------------------------------------------------------------------------
-- Table privileges.
--
-- These are a different mechanism from RLS and both must be right. GRANT decides
-- whether a role may attempt an operation on a table at all; RLS then decides
-- which rows it sees. Supabase's default privileges normally cover new tables in
-- public, but stating it explicitly keeps this file self-contained.
--
-- `anon` is the role for a visitor with no session. Vesta has nothing an anonymous
-- visitor should read, so anon is granted nothing at all.
-- ---------------------------------------------------------------------------
grant usage on schema public to anon, authenticated, service_role;

grant select, insert, update, delete on all tables in schema public to authenticated;
grant usage, select on all sequences in schema public to authenticated;

-- The Cloudflare Worker connects as service_role. It bypasses RLS, but bypassing
-- RLS is not the same as being allowed to touch the table at all, so it still
-- needs these grants. Without them file uploads fail with "permission denied",
-- and only Supabase's "automatically expose new tables" default privileges would
-- have been quietly covering for it.
grant select, insert, update, delete on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;

revoke all on all tables in schema public from anon;

-- allowed_emails is invisible to every client role. RLS already denies it, this
-- makes it a permission error rather than an empty result, and stops anyone
-- enumerating who has been invited.
revoke all on allowed_emails from anon, authenticated;

-- ---------------------------------------------------------------------------
-- Sanity check. Run this any time; it must return zero rows.
-- Any table listed here is a table the browser can read without restriction.
-- ---------------------------------------------------------------------------
-- select c.relname as table_without_rls
-- from pg_class c
-- join pg_namespace n on n.oid = c.relnamespace
-- where n.nspname = 'public'
--   and c.relkind = 'r'
--   and c.relrowsecurity = false;

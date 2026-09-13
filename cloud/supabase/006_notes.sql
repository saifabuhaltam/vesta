-- Vesta: notes as a document system rather than a text box.
--
-- Adds nested folders with a type, note flags (pinned, starred, ordering),
-- links from a note to the things it is about, and full text search over the
-- note body.

-- ---------------------------------------------------------------------------
-- Folders: nested, typed, ordered.
--
-- `parent_id` is what turns a flat list into a tree. `kind` exists because the
-- common folders in a course are always the same four, and knowing which is
-- which lets the interface give them the right icon and sort them sensibly
-- without the user naming them precisely.
-- ---------------------------------------------------------------------------
alter table note_folders add column if not exists parent_id uuid
  references note_folders(id) on delete cascade;
alter table note_folders add column if not exists kind text not null default 'custom';
alter table note_folders add column if not exists sort_order integer not null default 0;

do $$
begin
  alter table note_folders drop constraint if exists note_folders_kind_check;
  alter table note_folders add constraint note_folders_kind_check
    check (kind in ('lecture','reading','exam','assignment','custom'));
end;
$$;

create index if not exists note_folders_parent_idx on note_folders(parent_id);

-- A folder cannot be inside itself, at any depth. Without this a single bad
-- drag makes the tree impossible to render and impossible to fix from the UI.
create or replace function check_folder_cycle()
returns trigger
language plpgsql
as $$
declare
  cursor_id uuid := new.parent_id;
  hops integer := 0;
begin
  if new.parent_id is null then
    return new;
  end if;
  if new.parent_id = new.id then
    raise exception 'A folder cannot be its own parent.';
  end if;
  while cursor_id is not null loop
    hops := hops + 1;
    if hops > 50 then
      raise exception 'Folder nesting is too deep.';
    end if;
    if cursor_id = new.id then
      raise exception 'That would put a folder inside one of its own subfolders.';
    end if;
    select parent_id into cursor_id from note_folders where id = cursor_id;
  end loop;
  return new;
end;
$$;

drop trigger if exists note_folders_no_cycles on note_folders;
create trigger note_folders_no_cycles
  before insert or update of parent_id on note_folders
  for each row execute function check_folder_cycle();

-- ---------------------------------------------------------------------------
-- Note flags
-- ---------------------------------------------------------------------------
alter table notes add column if not exists pinned     boolean not null default false;
alter table notes add column if not exists starred    boolean not null default false;
alter table notes add column if not exists sort_order integer not null default 0;
alter table notes add column if not exists icon       text;

create index if not exists notes_pinned_idx
  on notes(user_id) where pinned and deleted_at is null;
create index if not exists notes_starred_idx
  on notes(user_id) where starred and deleted_at is null;

-- ---------------------------------------------------------------------------
-- note_links: what a note is about.
--
-- `notes.linked_item_id` allowed exactly one assignment. A revision note for a
-- midterm realistically points at three lecture PDFs, a reading, the exam entry
-- on the calendar and the assignment itself, so this is a proper many-to-many.
--
-- Separate nullable foreign keys rather than a type/id pair, so Postgres can
-- actually enforce the reference and cascade the delete.
-- ---------------------------------------------------------------------------
create table if not exists note_links (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null default auth.uid() references auth.users(id) on delete cascade,
  note_id           uuid not null references notes(id) on delete cascade,
  item_id           uuid references items(id) on delete cascade,
  file_id           uuid references files(id) on delete cascade,
  event_id          uuid references events(id) on delete cascade,
  headstart_id      uuid references headstarts(id) on delete cascade,
  schedule_entry_id uuid references schedule_entries(id) on delete cascade,  -- a lecture
  target_note_id    uuid references notes(id) on delete cascade,
  label             text not null default '',
  created_at        timestamptz not null default now(),
  constraint note_links_one_target check (
    (case when item_id           is not null then 1 else 0 end) +
    (case when file_id           is not null then 1 else 0 end) +
    (case when event_id          is not null then 1 else 0 end) +
    (case when headstart_id      is not null then 1 else 0 end) +
    (case when schedule_entry_id is not null then 1 else 0 end) +
    (case when target_note_id    is not null then 1 else 0 end) = 1
  ),
  constraint note_links_no_self check (target_note_id is null or target_note_id <> note_id)
);
create index if not exists note_links_user_idx on note_links(user_id);
create index if not exists note_links_note_idx on note_links(note_id);
create index if not exists note_links_item_idx on note_links(item_id);
create index if not exists note_links_file_idx on note_links(file_id);

-- the same thing should not be attached to the same note twice
create unique index if not exists note_links_unique_idx
  on note_links(note_id,
                coalesce(item_id, '00000000-0000-0000-0000-000000000000'::uuid),
                coalesce(file_id, '00000000-0000-0000-0000-000000000000'::uuid),
                coalesce(event_id, '00000000-0000-0000-0000-000000000000'::uuid),
                coalesce(headstart_id, '00000000-0000-0000-0000-000000000000'::uuid),
                coalesce(schedule_entry_id, '00000000-0000-0000-0000-000000000000'::uuid),
                coalesce(target_note_id, '00000000-0000-0000-0000-000000000000'::uuid));

-- Carry the old single link across, then leave the column in place so nothing
-- that still reads it breaks. New links go in note_links.
insert into note_links (user_id, note_id, item_id)
select n.user_id, n.id, n.linked_item_id
from notes n
where n.linked_item_id is not null
on conflict do nothing;

-- ---------------------------------------------------------------------------
-- Search across titles and contents.
--
-- The body is HTML, so the tags are stripped before indexing, otherwise a search
-- for "table" would match every note containing one. Generated and stored, so the
-- index stays correct without the application remembering to update it.
-- ---------------------------------------------------------------------------
alter table notes add column if not exists search_tsv tsvector
  generated always as (
    to_tsvector('english',
      regexp_replace(coalesce(title, '') || ' ' || coalesce(body, ''), '<[^>]*>', ' ', 'g'))
  ) stored;

create index if not exists notes_search_idx on notes using gin (search_tsv);

-- ---------------------------------------------------------------------------
-- Same rules as every other table.
-- ---------------------------------------------------------------------------
do $$
begin
  perform apply_owner_rls('note_links');
end;
$$;

grant select, insert, update, delete on note_links to authenticated, service_role;

-- 005 revoked execute on every function from client roles; keep it that way for
-- the one added here. PUBLIC first: anon and authenticated inherit through it, so
-- revoking only the named roles would leave this callable.
revoke execute on function check_folder_cycle() from public;
revoke execute on function check_folder_cycle() from anon, authenticated;

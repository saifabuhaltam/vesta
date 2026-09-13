-- Vesta: grade categories, flashcards and quizzes.
--
-- Runs after 002, because it calls apply_owner_rls() which that file defines.
--
-- Nothing here has a screen yet. The point is that the shape of the data is
-- settled before features are built on top of it, so adding the study tools later
-- does not mean a migration that touches everything.

-- ---------------------------------------------------------------------------
-- grade_categories
--
-- Grades were previously computed from a weight on each assignment. That works
-- until a syllabus says "Assignments 40%, Midterm 25%, Final 35%", which is how
-- courses are actually graded. A category holds the weight, assignments belong to
-- a category, and an assignment's own weight becomes its share within it.
--
-- `drop_lowest` handles the common "we drop your worst quiz" rule.
-- ---------------------------------------------------------------------------
create table if not exists grade_categories (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  class_id    uuid not null references classes(id) on delete cascade,
  name        text not null default '',
  weight      numeric not null default 0,      -- percent of the final grade
  drop_lowest integer not null default 0,      -- how many lowest scores to ignore
  sort_order  integer not null default 0,
  created_at  timestamptz not null default now()
);
create index if not exists grade_categories_user_idx  on grade_categories(user_id);
create index if not exists grade_categories_class_idx on grade_categories(class_id);

-- Assignments can now sit in a category. Nullable, so everything that already
-- exists keeps working exactly as it does today.
alter table items add column if not exists category_id uuid
  references grade_categories(id) on delete set null;
create index if not exists items_category_idx on items(category_id);

-- ---------------------------------------------------------------------------
-- flashcards
--
-- Scheduling fields follow the SM-2 family: each card carries its own ease and
-- interval, and `due_date` is what a study session queries. Keeping the schedule
-- on the card rather than in a separate review log means "what do I study today"
-- is one indexed lookup.
-- ---------------------------------------------------------------------------
create table if not exists flashcard_decks (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  class_id    uuid references classes(id) on delete cascade,
  name        text not null default 'Untitled deck',
  description text not null default '',
  -- where the deck came from, when it was generated rather than typed
  source_note_id uuid references notes(id) on delete set null,
  source_file_id uuid references files(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists decks_user_idx  on flashcard_decks(user_id);
create index if not exists decks_class_idx on flashcard_decks(class_id);

create table if not exists flashcards (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null default auth.uid() references auth.users(id) on delete cascade,
  deck_id          uuid not null references flashcard_decks(id) on delete cascade,
  front            text not null default '',
  back             text not null default '',
  -- spaced repetition state
  ease             numeric not null default 2.5,   -- SM-2 ease factor
  interval_days    integer not null default 0,
  repetitions      integer not null default 0,
  lapses           integer not null default 0,
  due_date         date not null default (now() at time zone 'utc')::date,
  last_reviewed_at timestamptz,
  suspended        boolean not null default false,
  sort_order       integer not null default 0,
  created_at       timestamptz not null default now()
);
create index if not exists flashcards_user_idx on flashcards(user_id);
create index if not exists flashcards_deck_idx on flashcards(deck_id);
-- the query a study session actually runs
create index if not exists flashcards_due_idx
  on flashcards(user_id, due_date) where not suspended;

-- ---------------------------------------------------------------------------
-- quizzes
--
-- Questions are rows rather than a blob on the quiz, so a single question can be
-- edited, reordered or reported on. Answers to an attempt stay as jsonb, because
-- they are only ever read back as a whole.
-- ---------------------------------------------------------------------------
create table if not exists quizzes (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  class_id    uuid references classes(id) on delete cascade,
  item_id     uuid references items(id) on delete set null,   -- the exam it prepares for
  title       text not null default 'Untitled quiz',
  source      text not null default 'manual'
              check (source in ('manual','headstart','file','note')),
  source_file_id uuid references files(id) on delete set null,
  source_note_id uuid references notes(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists quizzes_user_idx  on quizzes(user_id);
create index if not exists quizzes_class_idx on quizzes(class_id);

create table if not exists quiz_questions (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  quiz_id     uuid not null references quizzes(id) on delete cascade,
  kind        text not null default 'multiple_choice'
              check (kind in ('multiple_choice','true_false','short_answer')),
  prompt      text not null default '',
  choices     jsonb not null default '[]'::jsonb,   -- ["A","B","C","D"] for multiple choice
  answer      text not null default '',
  explanation text not null default '',
  sort_order  integer not null default 0
);
create index if not exists quiz_questions_user_idx on quiz_questions(user_id);
create index if not exists quiz_questions_quiz_idx on quiz_questions(quiz_id, sort_order);

create table if not exists quiz_attempts (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null default auth.uid() references auth.users(id) on delete cascade,
  quiz_id      uuid not null references quizzes(id) on delete cascade,
  responses    jsonb not null default '{}'::jsonb,  -- { question_id: given_answer }
  score        numeric,                             -- percent correct
  correct_count integer not null default 0,
  total_count   integer not null default 0,
  started_at   timestamptz not null default now(),
  completed_at timestamptz
);
create index if not exists quiz_attempts_user_idx on quiz_attempts(user_id);
create index if not exists quiz_attempts_quiz_idx on quiz_attempts(quiz_id, started_at desc);

-- ---------------------------------------------------------------------------
-- Same owner-only rules as everything else, and the same explicit grants, so
-- these tables do not depend on Supabase's "expose new tables" setting either.
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array[
    'grade_categories',
    'flashcard_decks',
    'flashcards',
    'quizzes',
    'quiz_questions',
    'quiz_attempts'
  ] loop
    perform apply_owner_rls(t);
  end loop;
end;
$$;

grant select, insert, update, delete on
  grade_categories, flashcard_decks, flashcards, quizzes, quiz_questions, quiz_attempts
  to authenticated, service_role;

-- keep updated_at honest on the two tables that have it
do $$
declare t text;
begin
  foreach t in array array['flashcard_decks','quizzes'] loop
    execute format('drop trigger if exists %I_touch on %I', t, t);
    execute format(
      'create trigger %I_touch before update on %I
       for each row execute function touch_updated_at()', t, t);
  end loop;
end;
$$;

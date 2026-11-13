# 03 — Database Schema (Supabase Postgres)

This is the design. The first Alembic migration implements it verbatim; after that, **changes go in new migrations and this doc is updated in the same PR**.

Added beyond the PDF table list: `round_candidates` (who is in which round, with what result) and `audit_logs` (NFR auditability). `users.id` = Supabase `auth.users.id`.

```sql
create extension if not exists pgcrypto;

create type user_role       as enum ('hr', 'super_admin');
create type drive_status    as enum ('draft', 'active', 'closed');
create type round_type      as enum ('shortlist', 'aptitude', 'coding', 'ai_interview', 'technical', 'final');
create type round_mode      as enum ('ai', 'manual', 'auto');
create type round_status    as enum ('pending', 'active', 'skipped', 'completed');
create type rc_status       as enum ('invited', 'in_progress', 'passed', 'failed', 'absent', 'selected', 'rejected');
create type difficulty      as enum ('easy', 'medium', 'advanced');
create type question_kind   as enum ('mcq', 'coding');
create type attempt_status  as enum ('not_started', 'in_progress', 'locked', 'submitted', 'expired');

create table users (
  id          uuid primary key,                       -- = auth.users.id
  email       text not null unique,
  full_name   text not null,
  role        user_role not null,
  is_active   boolean not null default true,
  created_at  timestamptz not null default now()
);

create table drives (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid not null references users(id),
  company     text not null,
  campus      text not null,
  role_title  text not null,
  jd_text     text not null,
  status      drive_status not null default 'draft',
  created_at  timestamptz not null default now()
);

create table candidates (
  id          uuid primary key default gen_random_uuid(),
  drive_id    uuid not null references drives(id) on delete cascade,
  student_id  text not null,
  dob         date not null,
  full_name   text not null,
  email       text not null,
  phone       text,
  form_data   jsonb not null default '{}',             -- raw Google Form row
  created_at  timestamptz not null default now(),
  unique (drive_id, student_id)
);

create table resumes (
  id             uuid primary key default gen_random_uuid(),
  candidate_id   uuid not null unique references candidates(id) on delete cascade,
  storage_path   text not null,                        -- Supabase Storage key
  extracted_text text,
  skills         text[] not null default '{}',
  cgpa           numeric(4,2),
  jd_match       numeric(5,2),                         -- 0..100
  parse_status   text not null default 'pending',      -- pending|ok|failed
  shortlisted    boolean,
  created_at     timestamptz not null default now()
);

create table rounds (
  id          uuid primary key default gen_random_uuid(),
  drive_id    uuid not null references drives(id) on delete cascade,
  position    int  not null,
  type        round_type not null,
  mode        round_mode not null default 'auto',
  status      round_status not null default 'pending',
  threshold   numeric(6,2),                            -- cut-off marks / score
  unique (drive_id, position)
);

create table round_candidates (
  round_id     uuid not null references rounds(id) on delete cascade,
  candidate_id uuid not null references candidates(id) on delete cascade,
  status       rc_status not null default 'invited',
  score        numeric(6,2),
  rank         int,
  hr_note      text,
  primary key (round_id, candidate_id)
);

create table tests (
  id             uuid primary key default gen_random_uuid(),
  round_id       uuid not null unique references rounds(id) on delete cascade,
  difficulty     difficulty,
  duration_sec   int  not null,
  question_count int  not null,
  settings       jsonb not null default '{}',          -- tags, per-level counts, languages
  opens_at       timestamptz,
  closes_at      timestamptz
);

create table questions (
  id            uuid primary key default gen_random_uuid(),
  kind          question_kind not null,
  body          text not null,
  options       jsonb,                                 -- mcq: ["A","B","C","D"]
  answer        jsonb,                                 -- mcq: index; NEVER sent to students
  tags          text[] not null default '{}',
  difficulty    difficulty not null,
  marks         numeric(5,2) not null default 1,
  ai_generated  boolean not null default false,
  approved      boolean not null default false,
  created_by    uuid references users(id),
  created_at    timestamptz not null default now(),
  explanation   text                                   -- 0002: worked solution / "why"; HR only
);

create table test_questions (
  test_id     uuid not null references tests(id) on delete cascade,
  question_id uuid not null references questions(id),
  position    int not null,
  primary key (test_id, question_id)
);

create table test_cases (
  id          uuid primary key default gen_random_uuid(),
  question_id uuid not null references questions(id) on delete cascade,
  input       text not null,
  expected    text not null,
  is_sample   boolean not null,                        -- 3 sample + 15 hidden
  position    int not null
);

create table attempts (
  id             uuid primary key default gen_random_uuid(),
  test_id        uuid not null references tests(id) on delete cascade,
  candidate_id   uuid not null references candidates(id) on delete cascade,
  status         attempt_status not null default 'not_started',
  started_at     timestamptz,
  deadline_at    timestamptz,                          -- server-owned (D5)
  submitted_at   timestamptz,
  remaining_sec_on_lock int,                           -- used when HR resumes
  tab_switches   int not null default 0,
  marks          numeric(6,2),
  time_taken_sec int,
  resume_count   int not null default 0,
  unique (test_id, candidate_id)
);

create table answers (
  id            uuid primary key default gen_random_uuid(),
  attempt_id    uuid not null references attempts(id) on delete cascade,
  question_id   uuid not null references questions(id),
  response      jsonb,                                 -- mcq choice
  code          text,                                  -- coding: last SUBMITTED code
  language      text,
  passed_cases  int,
  total_cases   int,
  marks         numeric(5,2),                          -- coding: set by submit-code
  updated_at    timestamptz not null default now(),    -- coding: last submit time
  draft_code     text,                                 -- 0006: coding draft (editor autosave)
  draft_language text,
  draft_saved_at timestamptz,
  unique (attempt_id, question_id)
);

create table interviews (
  id            uuid primary key default gen_random_uuid(),
  round_id      uuid not null references rounds(id) on delete cascade,
  candidate_id  uuid not null references candidates(id) on delete cascade,
  status        text not null default 'scheduled',     -- scheduled|live|completed|failed
  transcript    jsonb,                                 -- [{role: ai|candidate, kind: spoken|typed|question, text, language, ts}]
  ai_scores     jsonb,                                 -- {communication, technical_depth, problem_solving, culture: 1-5, sentiment{overall, confidence, note}, summary, model, evaluated_at} | {pending: true} | {error}
  hr_notes      text,
  started_at    timestamptz,
  ended_at      timestamptz,
  unique (round_id, candidate_id)
);

create table violations (
  id          uuid primary key default gen_random_uuid(),
  attempt_id  uuid not null references attempts(id) on delete cascade,
  kind        text not null,                           -- tab_switch|fullscreen_exit
  occurred_at timestamptz not null default now(),
  hr_action   text,                                    -- resumed|dismissed
  hr_by       uuid references users(id),
  hr_at       timestamptz
);

create table verification_logs (
  id           uuid primary key default gen_random_uuid(),
  candidate_id uuid not null references candidates(id) on delete cascade,
  round_id     uuid not null references rounds(id) on delete cascade,
  step         text not null,                          -- id_dob|location|terms|fullscreen
  ok           boolean not null,
  meta         jsonb not null default '{}',            -- lat/lng, user agent, ip
  created_at   timestamptz not null default now()
);

create table email_logs (
  id           uuid primary key default gen_random_uuid(),
  candidate_id uuid references candidates(id) on delete set null,
  round_id     uuid references rounds(id) on delete set null,
  to_email     text not null,
  template     text not null,
  status       text not null,                          -- queued|sent|failed
  error        text,
  created_at   timestamptz not null default now()
);

create table ai_prompts (
  key         text primary key,                        -- question_gen_mcq, interview_system, interview_eval…
  body        text not null,
  version     int not null default 1,
  updated_by  uuid references users(id),
  updated_at  timestamptz not null default now()
);

create table audit_logs (
  id          bigint generated always as identity primary key,
  actor_id    uuid references users(id),
  action      text not null,                           -- e.g. round.skip, answer_key.update
  entity      text not null,
  entity_id   text,
  diff        jsonb,
  created_at  timestamptz not null default now()
);

create index on candidates (drive_id);
create index on rounds (drive_id);
create index on round_candidates (candidate_id);
create index on attempts (test_id, status);
create index on violations (attempt_id);
create index on questions using gin (tags);

-- 0002_paper_builder: Round 1 paper builder (docs/07-round1-paper-builder-implementation-plan.md)
-- drive_ref / candidate_ref are opaque text, no FK (drives/candidates modules not built yet).
create table paper_drafts (
  id            uuid primary key default gen_random_uuid(),
  drive_ref     text not null,
  owner_id      uuid not null references users(id),
  title         text not null,
  track         text not null check (track in ('it', 'non_it')),
  settings      jsonb not null,                        -- PaperSettings
  pool          jsonb not null default '[]',           -- [PoolQuestion] incl. answer + why
  checks        jsonb not null default '[]',           -- [Check]
  status        text not null check (status in ('generating', 'draft', 'accepted', 'failed')),
  progress      jsonb not null default jsonb_build_object('done', 0, 'total', 0),
  error         text,                                  -- user-safe message when failed
  created_at    timestamptz not null default now(),
  accepted_at   timestamptz,
  persisted_at  timestamptz                            -- pool written to questions (first send)
);

create table paper_issues (
  draft_id      uuid not null references paper_drafts(id) on delete cascade,
  candidate_ref text not null,
  seed          text not null,                         -- drive_ref|track|candidate_ref (or fixed)
  issued_at     timestamptz not null default now(),
  primary key (draft_id, candidate_ref)
);

create index paper_drafts_owner_id_idx on paper_drafts (owner_id);
create index paper_drafts_drive_ref_idx on paper_drafts (drive_ref);
alter table paper_drafts enable row level security;
alter table paper_issues enable row level security;

-- 0004_question_bank: uploaded MCQ PDF banks + RAG embeddings (pgvector).
-- UNUSED since 2026-11-13: the question-bank code was removed (Technical questions are written by an
-- NVIDIA model). These tables and columns are kept so no uploaded bank is lost; nothing reads or writes them.
-- Dropping them needs a new migration.
create extension if not exists vector;

create table question_sources (                         -- one uploaded bank file
  id                uuid primary key default gen_random_uuid(),
  name              text not null,
  filename          text not null,                     -- original upload name
  storage_key       text not null,                     -- UUID; file at QBANK_DIR/<key>.pdf
  sha256            text not null unique,              -- duplicate-upload guard
  track             text not null check (track in ('it', 'non_it')),
  notes             text,
  status            text not null default 'ready' check (status in ('ready', 'failed')),
  is_active         boolean not null default true,     -- false = hidden from new papers
  uploaded_by       uuid not null references users(id),
  report            jsonb not null default '{}',       -- parser ParseReport
  embedding_status  text not null default 'pending'
                    check (embedding_status in ('pending', 'done', 'failed', 'skipped')),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

alter table questions add column source_id uuid references question_sources(id) on delete cascade;
alter table questions add column subject text;          -- bank subject heading
alter table questions add column topic text;
alter table questions add column source_number int;     -- number within its subject in the PDF
alter table questions add column warnings text[] not null default '{}';  -- parse warnings

create table question_embeddings (
  question_id  uuid primary key references questions(id) on delete cascade,
  embedding    vector(384) not null,                    -- = settings.embedding_dim (all-MiniLM-L6-v2)
  model        text not null
);

create index questions_source_id_idx on questions (source_id);
create index questions_subject_idx on questions (subject);
alter table question_sources enable row level security;
alter table question_embeddings enable row level security;

-- D8: RLS on, no policies → anon/authenticated keys read nothing. API uses the service connection.
do $$ declare t text; begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;
```

## Invariants the code must keep
- `attempts.deadline_at` is set once at start; only HR resume changes it (`now() + remaining_sec_on_lock`).
- `tab_switches >= 3` ⇒ `status = 'locked'` (set in the same statement that increments).
- Ranking: `order by marks desc, time_taken_sec asc`.
- `questions.answer` and `test_cases where is_sample = false` are only selected by HR/scoring code.
- `questions.explanation` and `paper_drafts.pool` (answers + why) are only selected by HR/Super Admin code.
- `questions.reference_solution` / `reference_language` are only selected by HR/Super Admin code (`app/execution/coding_questions/repo.py`); student queries (`GET /attempt`, run/submit) name their columns and never select them.
- `paper_drafts.pool` changes only while `status = 'draft'`; `accepted` is reached only from `draft` and freezes it.
- A pool question is written to `questions` once (same id, `on conflict (id) do nothing`), on the first send; `persisted_at` records it.
- `paper_issues` rows are never rewritten: sending again to the same candidate is a no-op.
- Bank questions (`questions.source_id is not null`) are inserted `approved = false`; only approved questions of `is_active` + `status = 'ready'` sources are retrieved for papers. Deleting a source hard-deletes its questions and embeddings (FK cascade); paper pools hold their own copies with new ids, so sent papers are unaffected.
- A draft in `generating` for more than 15 minutes is reported as `failed` ("Generation timed out") on read.
- Coding answers: submit-code sets `code`, `language`, `passed_cases`, `total_cases`, `updated_at` and `marks = round(questions.marks * passed / total, 2)` together, only while the attempt is `in_progress` or `locked` (row-locked), so the final submit's `attempts.marks = sum(answers.marks)` includes coding. A code draft only sets `draft_*`; the draft is newer than the submission when `draft_saved_at >= updated_at`.

## 0003_round_duration
`rounds."Duration" double precision` (nullable): minutes the round's test runs, saved from the drive wizard. The name is quoted with a capital D because it was first created by hand in Supabase; always write it as `"Duration"` in SQL. `rounds.threshold` stays the cut-off %.

## 0006_answer_code_drafts
`answers.draft_code text`, `answers.draft_language text`, `answers.draft_saved_at timestamptz` (all nullable): the coding editor's autosave (`PUT /attempt/code-drafts/{question_id}`). `answers.code` stays the last submitted code, which matches `passed_cases` and `marks`; saving a draft never changes those columns or `updated_at`.

## 0007_question_reference_solution
`questions.reference_solution text`, `questions.reference_language text` (both nullable, check `questions_reference_pair`: set together or both null). Written by `POST /questions/generate`: the program whose Judge0 stdout became each test case's `expected` (`reference_language` is `'python'`). Such questions are inserted `approved = false`, `ai_generated = true`. Manual coding questions leave both null. HR/Super Admin only.

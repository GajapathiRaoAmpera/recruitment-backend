"""init: schema from docs/03-database-schema.md

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# asyncpg runs ONE statement per op.execute, so the schema is a list of statements.
UPGRADE = [
    "create type user_role as enum ('hr', 'super_admin')",
    "create type drive_status as enum ('draft', 'active', 'closed')",
    "create type round_type as enum "
    "('shortlist', 'aptitude', 'coding', 'ai_interview', 'technical', 'final')",
    "create type round_mode as enum ('ai', 'manual', 'auto')",
    "create type round_status as enum ('pending', 'active', 'skipped', 'completed')",
    "create type rc_status as enum "
    "('invited', 'in_progress', 'passed', 'failed', 'absent', 'selected', 'rejected')",
    "create type difficulty as enum ('easy', 'medium', 'advanced')",
    "create type question_kind as enum ('mcq', 'coding')",
    "create type attempt_status as enum "
    "('not_started', 'in_progress', 'locked', 'submitted', 'expired')",
    """
    create table users (
      id          uuid primary key,
      email       text not null unique,
      full_name   text not null,
      role        user_role not null,
      is_active   boolean not null default true,
      created_at  timestamptz not null default now()
    )""",
    """
    create table drives (
      id          uuid primary key default gen_random_uuid(),
      owner_id    uuid not null references users(id),
      company     text not null,
      campus      text not null,
      role_title  text not null,
      jd_text     text not null,
      status      drive_status not null default 'draft',
      created_at  timestamptz not null default now()
    )""",
    """
    create table candidates (
      id          uuid primary key default gen_random_uuid(),
      drive_id    uuid not null references drives(id) on delete cascade,
      student_id  text not null,
      dob         date not null,
      full_name   text not null,
      email       text not null,
      phone       text,
      form_data   jsonb not null default '{}',
      created_at  timestamptz not null default now(),
      unique (drive_id, student_id)
    )""",
    """
    create table resumes (
      id             uuid primary key default gen_random_uuid(),
      candidate_id   uuid not null unique references candidates(id) on delete cascade,
      storage_path   text not null,
      extracted_text text,
      skills         text[] not null default '{}',
      cgpa           numeric(4,2),
      jd_match       numeric(5,2),
      parse_status   text not null default 'pending',
      shortlisted    boolean,
      created_at     timestamptz not null default now()
    )""",
    """
    create table rounds (
      id          uuid primary key default gen_random_uuid(),
      drive_id    uuid not null references drives(id) on delete cascade,
      position    int  not null,
      type        round_type not null,
      mode        round_mode not null default 'auto',
      status      round_status not null default 'pending',
      threshold   numeric(6,2),
      unique (drive_id, position)
    )""",
    """
    create table round_candidates (
      round_id     uuid not null references rounds(id) on delete cascade,
      candidate_id uuid not null references candidates(id) on delete cascade,
      status       rc_status not null default 'invited',
      score        numeric(6,2),
      rank         int,
      hr_note      text,
      primary key (round_id, candidate_id)
    )""",
    """
    create table tests (
      id             uuid primary key default gen_random_uuid(),
      round_id       uuid not null unique references rounds(id) on delete cascade,
      difficulty     difficulty,
      duration_sec   int  not null,
      question_count int  not null,
      settings       jsonb not null default '{}',
      opens_at       timestamptz,
      closes_at      timestamptz
    )""",
    """
    create table questions (
      id            uuid primary key default gen_random_uuid(),
      kind          question_kind not null,
      body          text not null,
      options       jsonb,
      answer        jsonb,
      tags          text[] not null default '{}',
      difficulty    difficulty not null,
      marks         numeric(5,2) not null default 1,
      ai_generated  boolean not null default false,
      approved      boolean not null default false,
      created_by    uuid references users(id),
      created_at    timestamptz not null default now()
    )""",
    """
    create table test_questions (
      test_id     uuid not null references tests(id) on delete cascade,
      question_id uuid not null references questions(id),
      position    int not null,
      primary key (test_id, question_id)
    )""",
    """
    create table test_cases (
      id          uuid primary key default gen_random_uuid(),
      question_id uuid not null references questions(id) on delete cascade,
      input       text not null,
      expected    text not null,
      is_sample   boolean not null,
      position    int not null
    )""",
    """
    create table attempts (
      id             uuid primary key default gen_random_uuid(),
      test_id        uuid not null references tests(id) on delete cascade,
      candidate_id   uuid not null references candidates(id) on delete cascade,
      status         attempt_status not null default 'not_started',
      started_at     timestamptz,
      deadline_at    timestamptz,
      submitted_at   timestamptz,
      remaining_sec_on_lock int,
      tab_switches   int not null default 0,
      marks          numeric(6,2),
      time_taken_sec int,
      resume_count   int not null default 0,
      unique (test_id, candidate_id)
    )""",
    """
    create table answers (
      id            uuid primary key default gen_random_uuid(),
      attempt_id    uuid not null references attempts(id) on delete cascade,
      question_id   uuid not null references questions(id),
      response      jsonb,
      code          text,
      language      text,
      passed_cases  int,
      total_cases   int,
      marks         numeric(5,2),
      updated_at    timestamptz not null default now(),
      unique (attempt_id, question_id)
    )""",
    """
    create table interviews (
      id            uuid primary key default gen_random_uuid(),
      round_id      uuid not null references rounds(id) on delete cascade,
      candidate_id  uuid not null references candidates(id) on delete cascade,
      status        text not null default 'scheduled',
      transcript    jsonb,
      ai_scores     jsonb,
      hr_notes      text,
      started_at    timestamptz,
      ended_at      timestamptz,
      unique (round_id, candidate_id)
    )""",
    """
    create table violations (
      id          uuid primary key default gen_random_uuid(),
      attempt_id  uuid not null references attempts(id) on delete cascade,
      kind        text not null,
      occurred_at timestamptz not null default now(),
      hr_action   text,
      hr_by       uuid references users(id),
      hr_at       timestamptz
    )""",
    """
    create table verification_logs (
      id           uuid primary key default gen_random_uuid(),
      candidate_id uuid not null references candidates(id) on delete cascade,
      round_id     uuid not null references rounds(id) on delete cascade,
      step         text not null,
      ok           boolean not null,
      meta         jsonb not null default '{}',
      created_at   timestamptz not null default now()
    )""",
    """
    create table email_logs (
      id           uuid primary key default gen_random_uuid(),
      candidate_id uuid references candidates(id) on delete set null,
      round_id     uuid references rounds(id) on delete set null,
      to_email     text not null,
      template     text not null,
      status       text not null,
      error        text,
      created_at   timestamptz not null default now()
    )""",
    """
    create table ai_prompts (
      key         text primary key,
      body        text not null,
      version     int not null default 1,
      updated_by  uuid references users(id),
      updated_at  timestamptz not null default now()
    )""",
    """
    create table audit_logs (
      id          bigint generated always as identity primary key,
      actor_id    uuid references users(id),
      action      text not null,
      entity      text not null,
      entity_id   text,
      diff        jsonb,
      created_at  timestamptz not null default now()
    )""",
    "create index on candidates (drive_id)",
    "create index on rounds (drive_id)",
    "create index on round_candidates (candidate_id)",
    "create index on attempts (test_id, status)",
    "create index on violations (attempt_id)",
    "create index on questions using gin (tags)",
    # D8: RLS on, no policies -> anon/authenticated keys read nothing; API uses the service role.
    """
    do $$ declare t text; begin
      for t in select tablename from pg_tables where schemaname = 'public' loop
        execute format('alter table %I enable row level security', t);
      end loop;
    end $$""",
]

TABLES = [  # children first, so drops never hit a dependent FK
    "audit_logs",
    "ai_prompts",
    "email_logs",
    "verification_logs",
    "violations",
    "interviews",
    "answers",
    "attempts",
    "test_cases",
    "test_questions",
    "questions",
    "tests",
    "round_candidates",
    "rounds",
    "resumes",
    "candidates",
    "drives",
    "users",
]
TYPES = [
    "attempt_status",
    "question_kind",
    "difficulty",
    "rc_status",
    "round_status",
    "round_mode",
    "round_type",
    "drive_status",
    "user_role",
]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    # pgcrypto is left installed: Supabase ships it and other objects may use it.
    for t in TABLES:
        op.execute(f"drop table if exists {t}")
    for t in TYPES:
        op.execute(f"drop type if exists {t}")

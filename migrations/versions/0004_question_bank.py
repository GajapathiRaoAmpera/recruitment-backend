"""question bank: question_sources, question_embeddings (pgvector), questions bank columns

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# asyncpg runs ONE statement per op.execute.
# No colon+word in this SQL: op.execute wraps it in text(), which reads that as a bind param.
# Embedding size 384 = settings.embedding_dim (all-MiniLM-L6-v2, run locally with fastembed);
# changing it needs a new migration that rebuilds question_embeddings.
UPGRADE = [
    """
    create table question_sources (
      id                uuid primary key default gen_random_uuid(),
      name              text not null,
      filename          text not null,
      storage_key       text not null,
      sha256            text not null unique,
      track             text not null check (track in ('it', 'non_it')),
      notes             text,
      status            text not null default 'ready' check (status in ('ready', 'failed')),
      is_active         boolean not null default true,
      uploaded_by       uuid not null references users(id),
      report            jsonb not null default '{}',
      embedding_status  text not null default 'pending'
                        check (embedding_status in ('pending', 'done', 'failed', 'skipped')),
      created_at        timestamptz not null default now(),
      updated_at        timestamptz not null default now()
    )""",
    "alter table questions add column source_id uuid "
    "references question_sources(id) on delete cascade",
    "alter table questions add column subject text",
    "alter table questions add column topic text",
    # question number inside its subject in the source PDF (display order)
    "alter table questions add column source_number int",
    # parse warnings kept with the question; 'approve all clean' skips non-empty ones
    "alter table questions add column warnings text[] not null default '{}'",
    "create index questions_source_id_idx on questions (source_id)",
    "create index questions_subject_idx on questions (subject)",
    # D8: RLS on, no policies -> anon/authenticated keys read nothing; API uses the service role.
    "alter table question_sources enable row level security",
]

DOWNGRADE = [
    "drop table if exists question_embeddings",
    "drop index if exists questions_subject_idx",
    "drop index if exists questions_source_id_idx",
    "alter table questions drop column if exists warnings",
    "alter table questions drop column if exists source_number",
    "alter table questions drop column if exists topic",
    "alter table questions drop column if exists subject",
    # drops the FK to question_sources too; bank questions themselves stay as plain questions
    "alter table questions drop column if exists source_id",
    "drop table if exists question_sources",
    # the vector extension is left installed on purpose (shared; other objects may use it)
]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    for sql in DOWNGRADE:
        op.execute(sql)

"""paper builder: paper_drafts, paper_issues, questions.explanation

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# asyncpg runs ONE statement per op.execute.
UPGRADE = [
    # the worked solution / "why" of a question (was nowhere in the schema)
    "alter table questions add column explanation text",
    """
    create table paper_drafts (
      id            uuid primary key default gen_random_uuid(),
      drive_ref     text not null,
      owner_id      uuid not null references users(id),
      title         text not null,
      track         text not null check (track in ('it', 'non_it')),
      settings      jsonb not null,
      pool          jsonb not null default '[]',
      checks        jsonb not null default '[]',
      status        text not null check (status in ('generating', 'draft', 'accepted', 'failed')),
      progress      jsonb not null default '{"done":0,"total":0}',
      error         text,
      created_at    timestamptz not null default now(),
      accepted_at   timestamptz,
      persisted_at  timestamptz
    )""",
    """
    create table paper_issues (
      draft_id      uuid not null references paper_drafts(id) on delete cascade,
      candidate_ref text not null,
      seed          text not null,
      issued_at     timestamptz not null default now(),
      primary key (draft_id, candidate_ref)
    )""",
    "create index paper_drafts_owner_id_idx on paper_drafts (owner_id)",
    "create index paper_drafts_drive_ref_idx on paper_drafts (drive_ref)",
    # D8: RLS on, no policies -> anon/authenticated keys read nothing; API uses the service role.
    "alter table paper_drafts enable row level security",
    "alter table paper_issues enable row level security",
]

DOWNGRADE = [
    "drop table if exists paper_issues",  # child first
    "drop table if exists paper_drafts",  # its indexes go with it
    "alter table questions drop column if exists explanation",
]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    for sql in DOWNGRADE:
        op.execute(sql)

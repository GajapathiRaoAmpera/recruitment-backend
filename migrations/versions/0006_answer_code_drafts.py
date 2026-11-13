"""answers: draft_code / draft_language / draft_saved_at for coding drafts (Round 2)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# A draft is what the editor holds; `code` stays the last SUBMITTED code (it matches
# passed_cases and marks). Saving a draft never touches code, passed_cases, total_cases, marks
# or updated_at, so updated_at stays the last submit time: the draft is the newer one when
# draft_saved_at >= updated_at. asyncpg runs ONE statement per op.execute.
UPGRADE = [
    "alter table answers add column draft_code text, add column draft_language text, "
    "add column draft_saved_at timestamptz",
]
DOWNGRADE = [
    "alter table answers drop column if exists draft_saved_at, "
    "drop column if exists draft_language, drop column if exists draft_code",
]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    for sql in DOWNGRADE:
        op.execute(sql)

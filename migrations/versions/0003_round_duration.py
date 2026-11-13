"""rounds."Duration": minutes a round's test runs (set in the drive wizard)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# The column was first added by hand in Supabase, spelled "Duration" (capital D, quoted).
# `if not exists` makes this a no-op there and creates the same column on a fresh database.
UPGRADE = [
    'alter table rounds add column if not exists "Duration" double precision',
]

DOWNGRADE = [
    'alter table rounds drop column if exists "Duration"',
]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    for sql in DOWNGRADE:
        op.execute(sql)

"""questions: reference_solution / reference_language for AI-generated coding questions (Round 2)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# POST /questions/generate stores the program whose Judge0 stdout became each test case's
# `expected`. HR/Super Admin only: student queries name their columns and never select these.
# Manual questions leave both null; the check keeps them set together. asyncpg runs ONE
# statement per op.execute.
UPGRADE = [
    "alter table questions add column reference_solution text, "
    "add column reference_language text, "
    "add constraint questions_reference_pair check "
    "((reference_solution is null) = (reference_language is null))",
]
DOWNGRADE = [
    "alter table questions drop constraint if exists questions_reference_pair, "
    "drop column if exists reference_language, drop column if exists reference_solution",
]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    for sql in DOWNGRADE:
        op.execute(sql)

"""questions: mode (stdin | function) + signature for function-style coding questions (Round 2)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-14
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# mode 'stdin': the student's whole program reads stdin (every question so far, the default).
# mode 'function': the student writes one function; a fixed driver (app/execution/function_mode)
# reads the case and prints the result. signature = {function_name, parameters:[{name, type}],
# return_type}, set exactly when mode = 'function'. Text + check (not an enum) keeps the
# downgrade a plain drop. asyncpg runs ONE statement per op.execute.
UPGRADE = [
    "alter table questions add column mode text not null default 'stdin', "
    "add column signature jsonb, "
    "add constraint questions_mode_check check (mode in ('stdin', 'function')), "
    "add constraint questions_signature_mode "
    "check ((mode = 'function') = (signature is not null))",
]
DOWNGRADE = [
    "alter table questions drop constraint if exists questions_signature_mode, "
    "drop constraint if exists questions_mode_check, "
    "drop column if exists signature, drop column if exists mode",
]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    for sql in DOWNGRADE:
        op.execute(sql)

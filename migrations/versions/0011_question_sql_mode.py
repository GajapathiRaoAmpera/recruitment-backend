"""questions: mode 'sql' + sql_schema + sql_order_matters for SQL coding questions (Round 2)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-14
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# mode 'sql': the student writes a SQLite query; Judge0 runs it once per dataset after a hidden
# setup (app/execution/sql_mode). sql_schema = the CREATE TABLE statements; sql_order_matters
# = whether row order is compared. Both are set exactly when mode = 'sql'. Each test case's
# `input` holds one dataset (INSERT statements) and `expected` the reference query's rows.
# asyncpg runs ONE statement per op.execute.
UPGRADE = [
    "alter table questions drop constraint questions_mode_check, "
    "add column sql_schema text, "
    "add column sql_order_matters boolean, "
    "add constraint questions_mode_check check (mode in ('stdin', 'function', 'sql')), "
    "add constraint questions_sql_mode "
    "check ((mode = 'sql') = (sql_schema is not null and sql_order_matters is not null))",
]
# Fails (check violation) while SQL questions exist: delete or convert them first, on purpose.
DOWNGRADE = [
    "alter table questions drop constraint if exists questions_sql_mode, "
    "drop column if exists sql_order_matters, drop column if exists sql_schema, "
    "drop constraint questions_mode_check, "
    "add constraint questions_mode_check check (mode in ('stdin', 'function'))",
]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    for sql in DOWNGRADE:
        op.execute(sql)

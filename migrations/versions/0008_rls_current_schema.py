"""RLS on every table in the app's schema (fixes 0001, whose loop only looked at 'public')

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-14
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# D8. On Azure the tables live in DB_SCHEMA (campushire), so 0001's loop missed them.
# Idempotent: where RLS is already on (Supabase/public) this changes nothing.
UPGRADE = [
    """
    do $$ declare t text; begin
      for t in select tablename from pg_tables where schemaname = current_schema() loop
        execute format('alter table %I enable row level security', t);
      end loop;
    end $$""",
]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    # No-op on purpose: RLS was meant to be on all along (D8); turning it off would weaken it.
    pass

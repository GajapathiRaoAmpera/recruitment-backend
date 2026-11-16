"""users: own password auth (replaces Supabase Auth) — password_hash, lockout columns, id default

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-14
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# password_hash: scrypt string from app/auth/passwords.py; null until the invite link is used.
# password_set_at: changes on every set; set-password tokens carry it, so each link works once.
# failed_logins/locked_until: 5 wrong passwords -> locked 15 min. asyncpg: ONE statement each.
UPGRADE = [
    "alter table users add column password_hash text, "
    "add column password_set_at timestamptz, "
    "add column failed_logins int not null default 0, "
    "add column locked_until timestamptz",
    # ids were Supabase Auth ids; now the DB makes them
    "alter table users alter column id set default gen_random_uuid()",
]
DOWNGRADE = [
    "alter table users alter column id drop default",
    "alter table users drop column if exists locked_until, "
    "drop column if exists failed_logins, drop column if exists password_set_at, "
    "drop column if exists password_hash",
]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    for sql in DOWNGRADE:
        op.execute(sql)

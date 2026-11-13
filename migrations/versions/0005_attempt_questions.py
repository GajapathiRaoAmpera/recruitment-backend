"""attempt_questions: each student's own question list and option order for an attempt

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# option_order[i] = index in questions.options of the option shown at position i (0-based).
# A saved answer (answers.response) is the SHOWN index, so it is scored through option_order.
# asyncpg runs ONE statement per op.execute.
UPGRADE = [
    """
    create table attempt_questions (
      attempt_id   uuid not null references attempts(id) on delete cascade,
      question_id  uuid not null references questions(id),
      position     int not null,
      option_order int[] not null,
      primary key (attempt_id, question_id)
    )""",
    "alter table attempt_questions enable row level security",
]
DOWNGRADE = ["drop table if exists attempt_questions"]


def upgrade() -> None:
    for sql in UPGRADE:
        op.execute(sql)


def downgrade() -> None:
    for sql in DOWNGRADE:
        op.execute(sql)

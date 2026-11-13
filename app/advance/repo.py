"""SQL for the cut-off preview and advance. HR queries filter by drive owner (None = SA)."""

import json
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

_ROUND = (
    "select r.id, r.drive_id, r.position, r.type, r.status, r.threshold "
    "from rounds r join drives d on d.id = r.drive_id "
    "where r.id = :r and (cast(:o as uuid) is null or d.owner_id = :o)"
)


async def get_round(
    session: AsyncSession, round_id: UUID, owner: UUID | None, lock: bool = False
) -> RowMapping | None:
    """The round if this HR owns its drive. lock=True holds the row until commit, so two
    advance clicks run one after the other (the second then sees 'completed')."""
    sql = _ROUND + (" for update of r" if lock else "")
    result = await session.execute(text(sql), {"r": round_id, "o": owner})
    return result.mappings().one_or_none()


async def attempts_past_deadline(session: AsyncSession, round_id: UUID) -> list[UUID]:
    result = await session.execute(
        text(
            "select a.id from attempts a join tests t on t.id = a.test_id "
            "where t.round_id = :r and a.status = 'in_progress' and a.deadline_at < now() "
            "order by a.id"
        ),
        {"r": round_id},
    )
    return list(result.scalars())


async def reopen_locked(session: AsyncSession, attempt_ids: list[UUID]) -> None:
    """Put locked attempts back to in_progress (same transaction) so the normal submit can
    finalize them; nothing is committed in between."""
    await session.execute(
        text(
            "update attempts set status = 'in_progress' "
            "where id = any(cast(:ids as uuid[])) and status = 'locked'"
        ),
        {"ids": attempt_ids},
    )


# One row per round_candidates row. tests.round_id and attempts(test_id, candidate_id) are
# unique, so there is at most one attempt. max_marks = what this student could earn: their own
# MCQs (attempt_questions) + the test's coding questions (test_questions), the same two lists
# GET /attempt shows them.
_ROWS = (
    "select c.id as candidate_id, c.student_id, c.full_name as name, rc.status as rc_status, "
    "a.id as attempt_id, cast(a.status as text) as attempt_status, a.marks, a.time_taken_sec, "
    "case when a.id is null then null else "
    "coalesce((select sum(q.marks) from attempt_questions aq "
    "join questions q on q.id = aq.question_id "
    "where aq.attempt_id = a.id and q.kind = 'mcq'), 0) + "
    "coalesce((select sum(q.marks) from test_questions tq "
    "join questions q on q.id = tq.question_id "
    "where tq.test_id = a.test_id and q.kind = 'coding'), 0) end as max_marks "
    "from round_candidates rc join candidates c on c.id = rc.candidate_id "
    "left join tests t on t.round_id = rc.round_id "
    "left join attempts a on a.test_id = t.id and a.candidate_id = rc.candidate_id "
    "where rc.round_id = :r"
)


async def round_rows(session: AsyncSession, round_id: UUID) -> list[RowMapping]:
    return list((await session.execute(text(_ROWS), {"r": round_id})).mappings())


async def save_results(session: AsyncSession, round_id: UUID, rows: list[dict]) -> None:
    """rows: [{c: candidate id, s: passed|failed|absent, score: percent|None, rank: int|None}]."""
    if not rows:
        return
    await session.execute(
        text(
            "update round_candidates rc set status = cast(x.s as rc_status), "
            "score = x.score, rank = x.rank "
            "from jsonb_to_recordset(cast(:rows as jsonb)) as x(c uuid, s text, score numeric, "
            "rank int) where rc.round_id = :r and rc.candidate_id = x.c"
        ),
        {"r": round_id, "rows": json.dumps(rows, default=str)},
    )


async def complete_round(session: AsyncSession, round_id: UUID, threshold) -> None:
    await session.execute(
        text("update rounds set status = 'completed', threshold = :t where id = :r"),
        {"r": round_id, "t": threshold},
    )


async def next_round(session: AsyncSession, drive_id: UUID, position: int) -> RowMapping | None:
    """The next round by position that HR hasn't skipped."""
    result = await session.execute(
        text(
            "select id, type, status from rounds where drive_id = :d and position > :p "
            "and status <> 'skipped' order by position limit 1 for update"
        ),
        {"d": drive_id, "p": position},
    )
    return result.mappings().one_or_none()


async def open_round(session: AsyncSession, round_id: UUID) -> None:
    await session.execute(
        text("update rounds set status = 'active' where id = :r and status = 'pending'"),
        {"r": round_id},
    )


async def invite(session: AsyncSession, round_id: UUID, candidate_ids: list[UUID]) -> list[UUID]:
    """Insert as invited; a candidate already in that round is left as is. Returns the new ones."""
    if not candidate_ids:
        return []
    result = await session.execute(
        text(
            "insert into round_candidates (round_id, candidate_id, status) "
            "select :r, x, 'invited' from unnest(cast(:ids as uuid[])) as x "
            "on conflict (round_id, candidate_id) do nothing returning candidate_id"
        ),
        {"r": round_id, "ids": candidate_ids},
    )
    return list(result.scalars())

from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

_ATTEMPT_COLS = (
    "a.id as attempt_id, a.candidate_id, c.student_id, c.full_name, "
    "case when a.status = 'in_progress' and a.deadline_at < now() then 'expired' "
    "else a.status::text end as status, a.started_at, a.submitted_at"
)


async def get_test(session: AsyncSession, test_id: UUID, owner_id: UUID | None) -> bool:
    """Is there such a test on a drive this HR owns (`owner_id` None = Super Admin)?"""
    rows = await session.execute(
        text(
            "select 1 from tests t join rounds r on r.id = t.round_id "
            "join drives d on d.id = r.drive_id "
            "where t.id = :id and (cast(:owner as uuid) is null or d.owner_id = :owner)"
        ),
        {"id": test_id, "owner": owner_id},
    )
    return rows.first() is not None


async def coding_questions(session: AsyncSession, test_id: UUID) -> list[RowMapping]:
    rows = await session.execute(
        text(
            "select q.id as question_id, tq.position, q.difficulty, q.marks "
            "from test_questions tq join questions q on q.id = tq.question_id "
            "where tq.test_id = :t and q.kind = 'coding' order by tq.position"
        ),
        {"t": test_id},
    )
    return list(rows.mappings())


async def attempts(session: AsyncSession, test_id: UUID, limit: int) -> list[RowMapping]:
    rows = await session.execute(
        text(
            f"select {_ATTEMPT_COLS} from attempts a join candidates c on c.id = a.candidate_id "
            "where a.test_id = :t order by a.started_at nulls last, c.full_name limit :limit"
        ),
        {"t": test_id, "limit": limit},
    )
    return list(rows.mappings())


async def attempt(session: AsyncSession, test_id: UUID, attempt_id: UUID) -> RowMapping | None:
    rows = await session.execute(
        text(
            f"select {_ATTEMPT_COLS} from attempts a join candidates c on c.id = a.candidate_id "
            "where a.test_id = :t and a.id = :a"
        ),
        {"t": test_id, "a": attempt_id},
    )
    return rows.mappings().one_or_none()


async def answers(
    session: AsyncSession, test_id: UUID, attempt_ids: list[UUID], *, with_code: bool
) -> list[RowMapping]:
    """Coding answers of these attempts, only for questions that are in the test."""
    extra = ", an.code, an.updated_at" if with_code else ""
    rows = await session.execute(
        text(
            "select an.attempt_id, an.question_id, an.language, an.passed_cases, "
            f"an.total_cases, an.marks{extra} from answers an "
            "join test_questions tq on tq.question_id = an.question_id and tq.test_id = :t "
            "join questions q on q.id = an.question_id and q.kind = 'coding' "
            "where an.attempt_id = any(:ids)"
        ),
        {"t": test_id, "ids": attempt_ids},
    )
    return list(rows.mappings())

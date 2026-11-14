from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.scoring import CaseSpec


async def get_attempt(session: AsyncSession, attempt_id: UUID) -> RowMapping | None:
    """The attempt's status plus whether its deadline has passed, by the database clock."""
    rows = await session.execute(
        text(
            "select status, coalesce(deadline_at <= now(), false) as past_deadline, "
            "(status = 'submitted' and submitted_at >= deadline_at) as auto_submitted "
            "from attempts where id = :id"
        ),
        {"id": attempt_id},
    )
    return rows.mappings().one_or_none()


async def is_coding_question_of_attempt(
    session: AsyncSession, attempt_id: UUID, question_id: UUID
) -> bool:
    rows = await session.execute(
        text(
            "select 1 from attempts a "
            "join test_questions tq on tq.test_id = a.test_id "
            "join questions q on q.id = tq.question_id "
            "where a.id = :attempt and q.id = :question and q.kind = 'coding'"
        ),
        {"attempt": attempt_id, "question": question_id},
    )
    return rows.first() is not None


async def load_cases(
    session: AsyncSession, question_id: UUID, *, samples_only: bool
) -> list[CaseSpec]:
    """Hidden cases (is_sample = false) are read only when `samples_only` is False, i.e. by
    Submit, which turns them into counts. Run never loads them."""
    where = "and is_sample" if samples_only else ""
    rows = await session.execute(
        text(
            "select input, expected, is_sample from test_cases "
            f"where question_id = :q {where} order by is_sample desc, position"
        ),
        {"q": question_id},
    )
    return [CaseSpec(r["input"], r["expected"], r["is_sample"]) for r in rows.mappings()]


async def save_submission(
    session: AsyncSession,
    attempt_id: UUID,
    question_id: UUID,
    code: str,
    language: str,
    passed: int,
    total: int,
) -> None:
    """Latest submission wins. `marks` is left alone: how a pass count turns into marks
    (level weighting) is still an open decision."""
    await session.execute(
        text(
            "insert into answers (attempt_id, question_id, code, language, passed_cases, "
            "total_cases) values (:a, :q, :code, :lang, :passed, :total) "
            "on conflict (attempt_id, question_id) do update set code = excluded.code, "
            "language = excluded.language, passed_cases = excluded.passed_cases, "
            "total_cases = excluded.total_cases, updated_at = now()"
        ),
        {
            "a": attempt_id,
            "q": question_id,
            "code": code,
            "lang": language,
            "passed": passed,
            "total": total,
        },
    )

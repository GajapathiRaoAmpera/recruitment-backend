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


async def function_signature(session: AsyncSession, question_id: UUID):
    """The question's signature (raw jsonb) when it is a function-mode question, else None."""
    rows = await session.execute(
        text("select signature from questions where id = :q and mode = 'function'"),
        {"q": question_id},
    )
    return rows.scalar_one_or_none()


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
) -> bool:
    """Latest submission wins, and sets the answer's marks: question marks x passed / total,
    rounded to 2 places (no level weighting). Final submit sums answers.marks into
    attempts.marks, so coding counts in the attempt total.

    Locks the attempt row first. If the attempt was finalized (submitted / expired) while
    Judge0 ran, nothing is saved and this returns False: attempts.marks is already summed."""
    status = await session.execute(
        text("select status from attempts where id = :a for update"), {"a": attempt_id}
    )
    if status.scalar_one_or_none() not in ("in_progress", "locked"):
        return False
    await session.execute(
        text(
            "insert into answers (attempt_id, question_id, code, language, passed_cases, "
            "total_cases, marks) "
            "select :a, q.id, :code, :lang, cast(:passed as int), cast(:total as int), "
            "round(q.marks * cast(:passed as int) / nullif(cast(:total as int), 0), 2) "
            "from questions q where q.id = :q "
            "on conflict (attempt_id, question_id) do update set code = excluded.code, "
            "language = excluded.language, passed_cases = excluded.passed_cases, "
            "total_cases = excluded.total_cases, marks = excluded.marks, updated_at = now()"
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
    return True


async def save_draft(
    session: AsyncSession, attempt_id: UUID, question_id: UUID, code: str, language: str
) -> None:
    """Editor autosave. Only the draft_* columns: `code` stays the last submitted code (it
    matches passed_cases and marks) and `updated_at` stays the last submit time."""
    await session.execute(
        text(
            "insert into answers (attempt_id, question_id, draft_code, draft_language, "
            "draft_saved_at) values (:a, :q, :code, :lang, now()) "
            "on conflict (attempt_id, question_id) do update set draft_code = excluded.draft_code, "
            "draft_language = excluded.draft_language, draft_saved_at = excluded.draft_saved_at"
        ),
        {"a": attempt_id, "q": question_id, "code": code, "lang": language},
    )


async def newer_drafts(session: AsyncSession, attempt_id: UUID) -> list[RowMapping]:
    """Drafts saved at or after the question's last submission (or with no submission yet)."""
    rows = await session.execute(
        text(
            "select question_id, draft_code, draft_language, draft_saved_at from answers "
            "where attempt_id = :a and draft_code is not null "
            "and (passed_cases is null or draft_saved_at >= updated_at)"
        ),
        {"a": attempt_id},
    )
    return list(rows.mappings())

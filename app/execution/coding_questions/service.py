"""Create / read / edit coding questions. Every mutation writes audit_logs in its transaction."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.core.audit import audit
from app.core.errors import AppError
from app.execution.coding_questions import repo
from app.execution.coding_questions.schemas import (
    CodingQuestionIn,
    CodingQuestionOut,
    CodingQuestionPatch,
    CodingQuestionSummary,
)


def _owner(user: StaffUser) -> UUID | None:
    """HR sees and edits their own questions; Super Admin sees all."""
    return None if user.role == "super_admin" else user.id


async def _load(session: AsyncSession, user: StaffUser, question_id: UUID):
    row = await repo.get_question(session, question_id, _owner(user))
    if row is None:  # also hides other HR's questions: same answer as "no such question"
        raise AppError(404, "QUESTION_NOT_FOUND", "Coding question not found")
    return row


async def _out(session: AsyncSession, row) -> CodingQuestionOut:
    cases = await repo.get_cases(session, row["id"])
    return CodingQuestionOut.model_validate({**dict(row), "test_cases": [dict(c) for c in cases]})


async def create(session: AsyncSession, user: StaffUser, body: CodingQuestionIn):
    row = await repo.insert_question(
        session, user.id, body.body, body.difficulty, body.tags, body.marks
    )
    await repo.insert_cases(session, row["id"], body.test_cases)
    # counts only: the audit log should not become a second copy of the hidden cases
    await audit(
        session,
        user.id,
        "question.create",
        "questions",
        row["id"],
        {"kind": "coding", "difficulty": body.difficulty, "test_cases": len(body.test_cases)},
    )
    await session.commit()
    return await _out(session, row)


async def get(session: AsyncSession, user: StaffUser, question_id: UUID) -> CodingQuestionOut:
    return await _out(session, await _load(session, user, question_id))


async def list_(
    session: AsyncSession,
    user: StaffUser,
    difficulty: str | None,
    tags: list[str] | None,
    approved: bool | None,
    limit: int,
) -> list[CodingQuestionSummary]:
    rows = await repo.list_questions(session, _owner(user), difficulty, tags, approved, limit)
    return [CodingQuestionSummary.model_validate(dict(r)) for r in rows]


async def update(
    session: AsyncSession, user: StaffUser, question_id: UUID, patch: CodingQuestionPatch
) -> CodingQuestionOut:
    current = await _load(session, user, question_id)
    changes = patch.model_dump(exclude={"test_cases"}, exclude_unset=True)
    if patch.test_cases is not None and await repo.has_answers(session, question_id):
        # shortcut: students already answered against the old cases; re-scoring them is not built
        raise AppError(
            409,
            "QUESTION_IN_USE",
            "Students have already answered this question, so its test cases can't change. "
            "Create a new question instead.",
        )
    row = current
    if changes:
        row = await repo.update_question(session, question_id, changes)
    if patch.test_cases is not None:
        await repo.delete_cases(session, question_id)
        await repo.insert_cases(session, question_id, patch.test_cases)
    await audit(
        session,
        user.id,
        "question.update",
        "questions",
        question_id,
        {"fields": sorted(changes), "test_cases_replaced": patch.test_cases is not None},
    )
    await session.commit()
    return await _out(session, row)

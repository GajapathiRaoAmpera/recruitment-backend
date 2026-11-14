"""Create / read / edit coding questions. Every mutation writes audit_logs in its transaction."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.core.audit import audit
from app.core.errors import AppError
from app.execution.coding_questions import generate as gen
from app.execution.coding_questions import repo
from app.execution.coding_questions.schemas import (
    CodingQuestionIn,
    CodingQuestionOut,
    CodingQuestionPatch,
    CodingQuestionSummary,
    GenerateFailure,
    GenerateIn,
    GenerateOut,
)
from app.execution.judge0 import Judge0Client
from app.papers.agents import LlmClient


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


async def generate(
    session: AsyncSession,
    user: StaffUser,
    body: GenerateIn,
    llm: LlmClient,
    judge0: Judge0Client,
) -> GenerateOut:
    """AI drafts (approved = false). Each good question is saved in its own transaction with its
    audit row; failed ones are reported, not saved."""
    avoid = await repo.recent_titles(session, user.id, gen.MAX_AVOID)
    await session.rollback()  # read only; frees the DB connection during the slow AI + Judge0 work
    drafts, failures = await gen.generate_many(
        llm, judge0, difficulty=body.difficulty, topic=body.topic, count=body.count, avoid=avoid
    )
    if not drafts:
        provider = next((f.error for f in failures if f.error is not None), None)
        if provider is not None and all(f.error is not None for f in failures):
            raise provider  # the AI provider itself failed (502 / 429), not its output
        raise AppError(
            502,
            "AI_BAD_OUTPUT",
            "The AI could not produce a usable question. Try again or change the topic.",
            {"failed": [{"index": f.index, "reason": f.reason} for f in failures]},
        )
    created = []
    for d in drafts:
        row = await repo.insert_question(
            session,
            user.id,
            d.question.body,
            body.difficulty,
            body.tags,
            body.marks,
            reference_solution=d.question.reference_solution,
            reference_language=gen.REFERENCE_LANGUAGE,
        )
        await repo.insert_cases(session, row["id"], d.cases)
        await audit(  # counts only, like question.create
            session,
            user.id,
            "question.generate",
            "questions",
            row["id"],
            {
                "kind": "coding",
                "difficulty": body.difficulty,
                "test_cases": len(d.cases),
                "reference_language": gen.REFERENCE_LANGUAGE,
                "model": getattr(llm, "model", None),
            },
        )
        await session.commit()
        created.append(await _out(session, row))
    return GenerateOut(
        created=created,
        failed=[GenerateFailure(index=f.index, reason=f.reason) for f in failures],
    )


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

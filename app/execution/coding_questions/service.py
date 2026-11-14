"""Create / read / edit coding questions. Every mutation writes audit_logs in its transaction."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.core.audit import audit
from app.core.errors import AppError
from app.execution.coding_questions import generate as gen
from app.execution.coding_questions import repo
from app.execution.coding_questions.schemas import (
    CaseIn,
    CodingQuestionIn,
    CodingQuestionOut,
    CodingQuestionPatch,
    CodingQuestionSummary,
    GenerateFailure,
    GenerateIn,
    GenerateOut,
    canonical_cases,
)
from app.execution.function_mode.signature import Signature, load_signature
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
    data = dict(row)
    data["mode"] = data.get("mode") or "stdin"
    data["signature"] = load_signature(data.get("signature"))
    return CodingQuestionOut.model_validate({**data, "test_cases": [dict(c) for c in cases]})


def _invalid(field: str, msg: str) -> AppError:
    """Same shape as a request validation error (the check needs the stored question)."""
    return AppError(
        422,
        "VALIDATION_ERROR",
        "Request is invalid",
        {"errors": [{"loc": ["body", field], "msg": msg}]},
    )


async def create(session: AsyncSession, user: StaffUser, body: CodingQuestionIn):
    row = await repo.insert_question(
        session,
        user.id,
        body.body,
        body.difficulty,
        body.tags,
        body.marks,
        mode=body.mode,
        signature=body.signature.model_dump() if body.signature else None,
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
        llm,
        judge0,
        difficulty=body.difficulty,
        topic=body.topic,
        count=body.count,
        avoid=avoid,
        mode=body.mode,
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
            mode=d.question.mode,
            signature=d.question.signature.model_dump() if d.question.signature else None,
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


def _resulting_shape(current, patch: CodingQuestionPatch) -> tuple[str, Signature | None]:
    """Mode and signature after the patch. Raises 422 when function mode has no signature."""
    sent = patch.model_fields_set
    mode = patch.mode if "mode" in sent else (current.get("mode") or "stdin")
    if mode == "stdin":
        return mode, None
    signature = patch.signature if "signature" in sent else load_signature(current.get("signature"))
    if signature is None:
        raise _invalid("signature", "a function-mode question needs a signature")
    return mode, signature


async def update(
    session: AsyncSession, user: StaffUser, question_id: UUID, patch: CodingQuestionPatch
) -> CodingQuestionOut:
    current = await _load(session, user, question_id)
    changes = patch.model_dump(exclude={"test_cases", "mode", "signature"}, exclude_unset=True)
    old_shape = (current.get("mode") or "stdin", load_signature(current.get("signature")))
    mode, signature = _resulting_shape(current, patch)
    shape_changed = (mode, signature) != old_shape
    if (patch.test_cases is not None or shape_changed) and await repo.has_answers(
        session, question_id
    ):
        # shortcut: students already answered against the old cases; re-scoring them is not built
        raise AppError(
            409,
            "QUESTION_IN_USE",
            "Students have already answered this question, so its test cases, mode and "
            "signature can't change. Create a new question instead.",
        )
    new_cases: list[CaseIn] | None = patch.test_cases
    if signature is not None and (new_cases is not None or shape_changed):
        if new_cases is None:  # new signature: the stored cases must still fit it
            stored = await repo.get_cases(session, question_id)
            new_cases = [
                CaseIn(input=c["input"], expected=c["expected"], is_sample=c["is_sample"])
                for c in stored
            ]
        try:
            new_cases = canonical_cases(signature, new_cases)
        except ValueError as e:
            raise _invalid("test_cases", str(e)) from None
    if shape_changed:
        changes["mode"] = mode
        changes["signature"] = signature.model_dump() if signature else None
    row = current
    if changes:
        row = await repo.update_question(session, question_id, changes)
    if new_cases is not None:
        await repo.delete_cases(session, question_id)
        await repo.insert_cases(session, question_id, new_cases)
    await audit(
        session,
        user.id,
        "question.update",
        "questions",
        question_id,
        {"fields": sorted(changes), "test_cases_replaced": new_cases is not None},
    )
    await session.commit()
    return await _out(session, row)

"""What each student scored on the coding questions of a test. Read-only, so no audit rows.

Score for a question = marks x passed / total (HR sets a higher `marks` on harder questions,
which is how level weighs in). An explicit `answers.marks`, if someone stored one, wins."""

from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.core.errors import AppError
from app.execution.results import repo
from app.execution.results.schemas import (
    AttemptSubmissions,
    CandidateResult,
    ResultQuestion,
    Submission,
    TestResults,
)

_CENTS = Decimal("0.01")
MAX_ROWS = 1000


def _max_score(questions) -> Decimal:
    return sum((Decimal(q["marks"]) for q in questions), Decimal(0)).quantize(_CENTS)


def _total(parts) -> Decimal:
    return sum(parts, Decimal(0)).quantize(_CENTS)


def _owner(user: StaffUser) -> UUID | None:
    return None if user.role == "super_admin" else user.id


def _earned(question, answer) -> Decimal:
    if answer is None:
        return Decimal(0).quantize(_CENTS)
    if answer["marks"] is not None:
        return Decimal(answer["marks"]).quantize(_CENTS, ROUND_HALF_UP)
    passed, total = answer["passed_cases"], answer["total_cases"]
    if not total or passed is None:
        return Decimal(0).quantize(_CENTS)
    return (Decimal(question["marks"]) * passed / total).quantize(_CENTS, ROUND_HALF_UP)


async def _check_test(session: AsyncSession, user: StaffUser, test_id: UUID) -> None:
    if not await repo.get_test(session, test_id, _owner(user)):  # also hides other HR's tests
        raise AppError(404, "TEST_NOT_FOUND", "Test not found")


def _answer_out(question, answer) -> dict:
    return dict(
        question_id=question["question_id"],
        submitted=answer is not None and answer["passed_cases"] is not None,
        language=answer["language"] if answer else None,
        passed=answer["passed_cases"] if answer else None,
        total=answer["total_cases"] if answer else None,
        marks_earned=_earned(question, answer),
    )


async def results(session: AsyncSession, user: StaffUser, test_id: UUID, limit: int) -> TestResults:
    await _check_test(session, user, test_id)
    questions = await repo.coding_questions(session, test_id)
    attempts = await repo.attempts(session, test_id, limit)
    rows = await repo.answers(
        session, test_id, [a["attempt_id"] for a in attempts], with_code=False
    )
    by_key = {(r["attempt_id"], r["question_id"]): r for r in rows}
    candidates = []
    for a in attempts:
        answers = [
            _answer_out(q, by_key.get((a["attempt_id"], q["question_id"]))) for q in questions
        ]
        candidates.append(
            CandidateResult(
                **dict(a),
                score=_total(x["marks_earned"] for x in answers),
                answers=answers,
            )
        )
    return TestResults(
        test_id=test_id,
        max_score=_max_score(questions),
        questions=[ResultQuestion.model_validate(dict(q)) for q in questions],
        candidates=candidates,
    )


async def submissions(
    session: AsyncSession, user: StaffUser, test_id: UUID, attempt_id: UUID
) -> AttemptSubmissions:
    await _check_test(session, user, test_id)
    a = await repo.attempt(session, test_id, attempt_id)
    if a is None:
        raise AppError(404, "ATTEMPT_NOT_FOUND", "Attempt not found")
    questions = await repo.coding_questions(session, test_id)
    rows = {
        r["question_id"]: r
        for r in await repo.answers(session, test_id, [attempt_id], with_code=True)
    }
    subs = []
    for q in questions:
        row = rows.get(q["question_id"])
        subs.append(
            Submission(
                **_answer_out(q, row),
                code=row["code"] if row else None,
                updated_at=row["updated_at"] if row else None,
            )
        )
    return AttemptSubmissions(
        **dict(a),
        score=_total(s.marks_earned for s in subs),
        max_score=_max_score(questions),
        submissions=subs,
    )

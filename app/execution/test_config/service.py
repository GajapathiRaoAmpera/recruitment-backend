"""Set up a round's test and choose its questions. Every mutation writes audit_logs in its
transaction. A test's questions are fixed once any student has started it."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.attempts.repo import DEFAULT_MINUTES
from app.auth.schemas import StaffUser
from app.core.audit import audit
from app.core.errors import AppError
from app.execution.test_config import repo
from app.execution.test_config.schemas import (
    TestConfigIn,
    TestConfigOut,
    TestQuestionOut,
    TestQuestionsOut,
)

# rounds whose students sit a test; the others (shortlist, interview, final) have none
TEST_ROUND_TYPES = {"aptitude", "coding", "technical"}


def _owner(user: StaffUser) -> UUID | None:
    """HR works on their own drives; Super Admin on all."""
    return None if user.role == "super_admin" else user.id


async def configure(
    session: AsyncSession, user: StaffUser, round_id: UUID, body: TestConfigIn
) -> TestConfigOut:
    """Create the round's test if it doesn't exist yet, then apply the fields that were sent."""
    rnd = await repo.get_round(session, round_id, _owner(user))
    if rnd is None:  # also hides other HR's rounds
        raise AppError(404, "ROUND_NOT_FOUND", "Round not found")
    if rnd["type"] not in TEST_ROUND_TYPES:
        raise AppError(409, "ROUND_HAS_NO_TEST", f"A {rnd['type']} round has no test to set up")
    changes = body.model_dump(exclude_unset=True)
    default_seconds = round((rnd["duration_min"] or DEFAULT_MINUTES) * 60)
    await repo.ensure_test(session, round_id, default_seconds)
    if changes:
        await repo.update_test(session, round_id, changes)
    row = await repo.get_test_of_round(session, round_id)
    await audit(
        session,
        user.id,
        "test.configure",
        "tests",
        row["id"],
        {"round_id": str(round_id), "fields": sorted(changes)},
    )
    await session.commit()
    return TestConfigOut.model_validate(dict(row))


def _levels(rows) -> dict[str, int]:
    counts = {"easy": 0, "medium": 0, "advanced": 0}
    for r in rows:
        counts[r["difficulty"]] += 1
    return counts


def _out(test_id: UUID, rows) -> TestQuestionsOut:
    return TestQuestionsOut(
        test_id=test_id,
        question_count=len(rows),
        levels=_levels(rows),
        questions=[TestQuestionOut.model_validate(dict(r)) for r in rows],
    )


async def set_questions(
    session: AsyncSession, user: StaffUser, test_id: UUID, question_ids: list[UUID]
) -> TestQuestionsOut:
    """Replace the test's question list. Each question's level is its own difficulty."""
    test = await repo.lock_test(session, test_id, _owner(user))
    if test is None:  # also hides other HR's tests
        raise AppError(404, "TEST_NOT_FOUND", "Test not found")

    current = await repo.current_questions(session, test_id)
    if [r["question_id"] for r in current] == question_ids:
        return _out(test_id, current)  # same list again (a retry): nothing to change

    if await repo.has_attempts(session, test_id):
        raise AppError(
            409,
            "TEST_IN_USE",
            "Students have already started this test, so its questions can't change.",
        )

    found = {r["id"]: r for r in await repo.usable_questions(session, question_ids, _owner(user))}
    missing = [str(q) for q in question_ids if q not in found]
    if missing:
        raise AppError(
            422,
            "QUESTIONS_NOT_FOUND",
            "Some questions don't exist, aren't coding questions, or aren't yours",
            {"question_ids": missing},
        )
    unapproved = [str(q) for q in question_ids if not found[q]["approved"]]
    if unapproved:
        raise AppError(
            422,
            "QUESTIONS_NOT_APPROVED",
            "Approve these questions first",
            {"question_ids": unapproved},
        )
    incomplete = [str(q) for q in question_ids if not found[q]["complete"]]
    if incomplete:
        raise AppError(
            422,
            "QUESTIONS_INCOMPLETE",
            "These questions need exactly 3 sample and 15 hidden test cases",
            {"question_ids": incomplete},
        )

    chosen = [found[q] for q in question_ids]
    levels = _levels(chosen)
    await repo.replace_questions(session, test_id, question_ids)
    await repo.set_counts(session, test_id, len(question_ids), levels)
    await audit(
        session,
        user.id,
        "test.set_questions",
        "tests",
        test_id,
        {
            "before": [str(r["question_id"]) for r in current],
            "after": [str(q) for q in question_ids],
            "levels": levels,
        },
    )
    await session.commit()
    return _out(
        test_id,
        [
            {
                "question_id": q,
                "position": i,
                "difficulty": found[q]["difficulty"],
                "marks": found[q]["marks"],
            }
            for i, q in enumerate(question_ids)
        ],
    )

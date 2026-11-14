"""Run and Submit for one candidate's coding question (FR-16, AC-5)."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.execution import repo, scoring
from app.execution.deps import AttemptContext
from app.execution.judge0 import Judge0Client
from app.execution.schemas import CaseResult, CodeRequest, RunOut, SubmitCodeOut

# shortcut: in-process, so it holds for one API process. With several workers or replicas
# the guard needs a shared lock (Redis / advisory lock); Judge0 limits still bound the damage.
_in_flight: set[UUID] = set()


@contextmanager
def one_run_at_a_time(candidate_id: UUID) -> Iterator[None]:
    if candidate_id in _in_flight:
        raise AppError(429, "RUN_IN_PROGRESS", "Wait for your current run to finish")
    _in_flight.add(candidate_id)
    try:
        yield
    finally:
        _in_flight.discard(candidate_id)


async def _check_can_run(session: AsyncSession, ctx: AttemptContext, question_id: UUID) -> None:
    attempt = await repo.get_attempt(session, ctx.attempt_id)
    if attempt is None:
        raise AppError(404, "ATTEMPT_NOT_FOUND", "Attempt not found")
    if (
        attempt["status"] == "expired"
        or attempt.get("auto_submitted")  # the deadline already submitted it
        or (attempt["status"] == "in_progress" and attempt["past_deadline"])
    ):
        raise AppError(409, "ATTEMPT_EXPIRED", "Time is up for this test")
    if attempt["status"] != "in_progress":
        raise AppError(409, "ATTEMPT_LOCKED", "This attempt is not open for answers")
    if not await repo.is_coding_question_of_attempt(session, ctx.attempt_id, question_id):
        raise AppError(404, "QUESTION_NOT_FOUND", "Coding question not found in this test")


async def run(
    session: AsyncSession, client: Judge0Client, ctx: AttemptContext, body: CodeRequest
) -> RunOut:
    """Run button: the 3 sample cases, with a result for each. Hidden cases are never loaded."""
    with one_run_at_a_time(ctx.candidate_id):
        await _check_can_run(session, ctx, body.question_id)
        cases = await repo.load_cases(session, body.question_id, samples_only=True)
        await session.commit()  # end the read transaction: Judge0 can take up to 15 s
        judged = await scoring.run_samples(client, body.language, body.code, cases)
    return RunOut(results=[CaseResult.of(i, j) for i, j in enumerate(judged, start=1)])


async def submit_code(
    session: AsyncSession, client: Judge0Client, ctx: AttemptContext, body: CodeRequest
) -> SubmitCodeOut:
    """Submit button: all cases count; the student gets `passed` out of `total` and nothing
    else. The pass count is saved on the answer (latest submission wins)."""
    with one_run_at_a_time(ctx.candidate_id):
        await _check_can_run(session, ctx, body.question_id)
        cases = await repo.load_cases(session, body.question_id, samples_only=False)
        if not cases:
            raise AppError(409, "NO_TEST_CASES", "This question has no test cases yet")
        await session.commit()
        result = await scoring.submit(client, body.language, body.code, cases)
    # a Judge0 outage raised above, so nothing is saved and the old result stays
    await repo.save_submission(
        session,
        ctx.attempt_id,
        body.question_id,
        body.code,
        body.language,
        result.passed,
        result.total,
    )
    await session.commit()
    return SubmitCodeOut(passed=result.passed, total=result.total)

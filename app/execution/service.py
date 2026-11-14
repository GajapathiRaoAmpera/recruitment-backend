"""Run and Submit for one candidate's coding question (FR-16, AC-5)."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.execution import repo, scoring
from app.execution.deps import AttemptContext
from app.execution.function_mode.drivers import build_source
from app.execution.function_mode.signature import load_signature
from app.execution.judge0 import Judge0Client
from app.execution.schemas import (
    CaseResult,
    CodeRequest,
    DraftIn,
    DraftOut,
    DraftsOut,
    RunOut,
    SubmitCodeOut,
)

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


async def _source(session: AsyncSession, body: CodeRequest) -> str:
    """What Judge0 runs: the student's code as is (stdin mode), or the student's function
    followed by the hidden driver (function mode; 422 LANGUAGE_NOT_ALLOWED for SQL).
    The combined source is never returned to the student."""
    signature = load_signature(await repo.function_signature(session, body.question_id))
    if signature is None:
        return body.code
    return build_source(body.language, signature, body.code)


async def run(
    session: AsyncSession, client: Judge0Client, ctx: AttemptContext, body: CodeRequest
) -> RunOut:
    """Run button: the 3 sample cases, with a result for each. Hidden cases are never loaded."""
    with one_run_at_a_time(ctx.candidate_id):
        await _check_can_run(session, ctx, body.question_id)
        source = await _source(session, body)
        cases = await repo.load_cases(session, body.question_id, samples_only=True)
        await session.commit()  # end the read transaction: Judge0 can take up to 15 s
        judged = await scoring.run_samples(client, body.language, source, cases)
    return RunOut(results=[CaseResult.of(i, j) for i, j in enumerate(judged, start=1)])


async def submit_code(
    session: AsyncSession, client: Judge0Client, ctx: AttemptContext, body: CodeRequest
) -> SubmitCodeOut:
    """Submit button: all cases count; the student gets `passed` out of `total` and nothing
    else. The pass count is saved on the answer (latest submission wins)."""
    with one_run_at_a_time(ctx.candidate_id):
        await _check_can_run(session, ctx, body.question_id)
        source = await _source(session, body)
        cases = await repo.load_cases(session, body.question_id, samples_only=False)
        if not cases:
            raise AppError(409, "NO_TEST_CASES", "This question has no test cases yet")
        await session.commit()
        result = await scoring.submit(client, body.language, source, cases)
    # a Judge0 outage raised above, so nothing is saved and the old result stays
    saved = await repo.save_submission(
        session,
        ctx.attempt_id,
        body.question_id,
        body.code,
        body.language,
        result.passed,
        result.total,
    )
    if not saved:  # the attempt was finalized while Judge0 ran: its total is already fixed
        await session.rollback()
        await _check_can_run(session, ctx, body.question_id)  # raises EXPIRED or LOCKED
        raise AppError(409, "ATTEMPT_LOCKED", "This attempt is not open for answers")
    await session.commit()
    return SubmitCodeOut(passed=result.passed, total=result.total)


async def save_draft(
    session: AsyncSession, ctx: AttemptContext, question_id: UUID, body: DraftIn
) -> None:
    """Code draft (FR-17, AC-6): same open-attempt and question checks as Run, no Judge0.
    Never overwrites the submitted code, its pass count or its marks."""
    await _check_can_run(session, ctx, question_id)
    await repo.save_draft(session, ctx.attempt_id, question_id, body.code, body.language)
    await session.commit()


async def drafts(session: AsyncSession, ctx: AttemptContext) -> DraftsOut:
    """Refresh recovery: drafts newer than their last submission, in any attempt status."""
    rows = await repo.newer_drafts(session, ctx.attempt_id)
    return DraftsOut(
        drafts={
            str(r["question_id"]): DraftOut(
                code=r["draft_code"], language=r["draft_language"], saved_at=r["draft_saved_at"]
            )
            for r in rows
        }
    )

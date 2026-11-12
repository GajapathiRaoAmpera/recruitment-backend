from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.attempts import state_repo as repo
from app.attempts.state_schemas import AttemptOut, SubmitOut
from app.core.config import get_settings
from app.core.errors import AppError

MAX_TAB_SWITCHES = 3


async def state(session: AsyncSession, attempt_id: UUID, candidate_id: UUID) -> AttemptOut:
    row = await repo.get_state(session, attempt_id, candidate_id)
    if row is None:
        raise AppError(404, "ATTEMPT_NOT_FOUND", "Attempt not found")
    # TEMPORARY: until the question bank links questions to a test, a dev server gives an empty
    # test the temp-seed questions. Exactly APP_ENV == "dev", never by default elsewhere.
    if get_settings().app_env == "dev":
        if await repo.attach_temp_questions(session, row["test_id"]):
            await session.commit()
    status = "expired" if row["past_deadline"] else row["status"]
    return AttemptOut(
        status=status,
        deadline_at=row["deadline_at"],
        server_now=row["server_now"],
        tab_switches=row["tab_switches"],
        max_tab_switches=MAX_TAB_SWITCHES,
        remaining_sec_on_lock=row["remaining_sec_on_lock"],
        questions=await repo.questions_for(session, row["test_id"]),
        saved_answers=await repo.saved_answers(session, attempt_id),
    )


async def answer(session: AsyncSession, attempt_id: UUID, question_id: UUID, response: int):
    outcome = await repo.save_answer(session, attempt_id, question_id, response)
    if outcome == "ok":
        await session.commit()
        return
    raise {
        "no_question": AppError(404, "QUESTION_NOT_FOUND", "Question not found"),
        "bad_option": AppError(422, "BAD_OPTION", "That option doesn't exist"),
        "closed": AppError(409, "ATTEMPT_LOCKED", "This attempt is not open for answers"),
        "expired": AppError(409, "ATTEMPT_EXPIRED", "Time is up for this test"),
    }[outcome]


async def submit(session: AsyncSession, attempt_id: UUID) -> SubmitOut:
    """Final submit, and what the deadline's auto-submit calls. Repeating it is harmless."""
    done = await repo.submit(session, attempt_id)
    if done is not None:
        await session.commit()
        return SubmitOut(status="submitted", time_taken_sec=done["time_taken_sec"])
    row = await repo.current_status(session, attempt_id)
    if row is None:
        raise AppError(404, "ATTEMPT_NOT_FOUND", "Attempt not found")
    if row["status"] == "submitted":  # already final: same answer, nothing changes
        return SubmitOut(status="submitted", time_taken_sec=row["time_taken_sec"])
    raise AppError(409, "ATTEMPT_LOCKED", "This attempt is paused. Ask HR to let you resume")

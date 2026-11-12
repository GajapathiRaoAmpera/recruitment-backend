from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.attempts import repo
from app.attempts.schemas import StartOut
from app.auth import entry
from app.auth import repo as auth_repo
from app.core.errors import AppError


async def start(session: AsyncSession, who: entry.Entry, round_id: UUID) -> StartOut:
    if await auth_repo.get_invite_target(session, who.candidate_id, round_id) is None:
        raise AppError(403, "NOT_INVITED", "You are not invited to this round")
    # invited, but not a round that can be sat right now (closed, finished, interview, ...)
    rnd = await repo.get_startable(session, who.candidate_id, round_id)
    if rnd is None:
        raise AppError(409, "ROUND_NOT_OPEN", "This round can't be started right now")
    existing = await repo.find_attempt(session, round_id, who.candidate_id)
    if existing is not None:
        # never reopen a finished attempt; HR's resume is what reopens a locked one
        if existing["status"] in ("submitted", "locked"):
            raise AppError(409, "ATTEMPT_LOCKED", "This attempt is already closed")
        if existing["status"] == "expired" or (
            existing["status"] == "in_progress" and existing["past_deadline"]
        ):
            raise AppError(409, "ATTEMPT_EXPIRED", "Time is up for this test")
    missing = await repo.missing_steps(session, who.candidate_id, round_id)
    if missing:
        raise AppError(409, "STEPS_MISSING", "Finish the system check first", {"missing": missing})
    test = await repo.ensure_test(session, round_id, rnd["minutes"])
    attempt_id = await repo.start_attempt(
        session, test["id"], who.candidate_id, round_id, test["duration_sec"]
    )
    await session.commit()
    return StartOut(
        attempt_token=entry.issue_attempt_token(attempt_id, who.candidate_id),
        attempt_id=attempt_id,
    )

"""HR resume (AC-7): reopen an attempt the 3rd tab switch locked, with the time it had left.

The switch counter restarts at 0, so the student again gets 3 switches before the next lock.
History is kept: every `violations` row stays, and HR's progress view counts those rows, so
the total HR sees never goes down."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.audit import audit
from app.core.db import get_session
from app.core.errors import AppError
from app.integrity import repo

router = APIRouter(prefix="/attempts", tags=["integrity"])


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class ResumeOut(BaseModel):
    attempt_id: UUID
    status: Literal["in_progress"]
    deadline_at: datetime
    tab_switches: int


@router.post("/{attempt_id}/resume", response_model=ResumeOut)
async def resume_attempt(
    attempt_id: UUID,
    body: ResumeIn,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
) -> ResumeOut:
    owner = None if user.role == "super_admin" else user.id  # HR: only their own drives
    found = await repo.find_for_resume(session, attempt_id, owner)
    if found is None:
        raise AppError(404, "ATTEMPT_NOT_FOUND", "Attempt not found")
    now = await repo.lock_for_resume(session, attempt_id, found["round_id"])
    if now["status"] != "locked":
        raise AppError(409, "NOT_LOCKED", "Only a locked attempt can be resumed")
    if now["round_status"] != "active":
        raise AppError(409, "ROUND_NOT_ACTIVE", "This round is not open any more")
    if now["remaining_sec_on_lock"] is None:
        # never guess the time left: finalize it from advance (finalize_locked) instead
        raise AppError(
            409,
            "REMAINING_TIME_UNKNOWN",
            "The time left at the lock was not recorded, so this attempt can't be resumed",
        )
    done = await repo.resume(session, attempt_id)
    if done is None:  # cannot happen while the rows are locked; refuse rather than half-do it
        raise AppError(409, "NOT_LOCKED", "Only a locked attempt can be resumed")
    await audit(
        session,
        user.id,
        "attempt.resume",
        "attempt",
        attempt_id,
        {
            "reason": body.reason,
            "previous_tab_switches": now["tab_switches"],
            "restored_sec": now["remaining_sec_on_lock"],
            "deadline_at": done["deadline_at"],
        },
    )
    await session.commit()
    return ResumeOut(
        attempt_id=attempt_id,
        status="in_progress",
        deadline_at=done["deadline_at"],
        tab_switches=done["tab_switches"],
    )

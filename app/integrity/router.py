from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.attempts.state_service import MAX_TAB_SWITCHES
from app.core.db import get_session
from app.core.errors import AppError
from app.execution.deps import AttemptContext, student_attempt
from app.integrity import repo

router = APIRouter(prefix="/attempt", tags=["integrity"])


class ViolationIn(BaseModel):
    kind: Literal["tab_switch", "fullscreen_exit"]


class ViolationOut(BaseModel):
    tab_switches: int
    locked: bool


@router.post("/violations", response_model=ViolationOut)
async def report_violation(
    body: ViolationIn,
    ctx: AttemptContext = Depends(student_attempt),
    session: AsyncSession = Depends(get_session),
):
    """Every event is logged. Only a tab switch counts toward the lock (3 closes the test).
    A network drop can look like a switch, so this only ever locks; it never fails the test."""
    current = await repo.current(session, ctx.attempt_id)
    if current is None:
        raise AppError(404, "ATTEMPT_NOT_FOUND", "Attempt not found")
    if body.kind == "tab_switch":
        changed = await repo.tab_switch(session, ctx.attempt_id, MAX_TAB_SWITCHES)
        if changed is not None:
            current = changed
    await repo.log_violation(session, ctx.attempt_id, body.kind)
    await session.commit()
    return {"tab_switches": current["tab_switches"], "locked": current["status"] == "locked"}

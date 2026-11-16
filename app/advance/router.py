from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.advance import service
from app.advance.schemas import (
    AdvanceIn,
    AdvanceOut,
    DecideIn,
    DecideOut,
    DecisionsOut,
    RankingOut,
)
from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session
from app.notifications import round_invites

router = APIRouter(tags=["rounds"])


@router.get("/rounds/{round_id}/ranking", response_model=RankingOut)
async def round_ranking(
    round_id: UUID,
    threshold: float | None = Query(None, ge=0, le=100),
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    """Preview for the cut-off slider. No threshold → the round's saved one, or none."""
    return await service.preview(session, user, round_id, threshold)


@router.post("/rounds/{round_id}/advance", response_model=AdvanceOut)
async def advance_round(
    round_id: UUID,
    body: AdvanceIn,
    tasks: BackgroundTasks,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    """Close this round on the cut-off and invite the passed students into the next one."""
    out = await service.advance(session, user, round_id, body)
    ids = [i.candidate_id for i in out.invited]
    mails = await round_invites.for_round(session, out.next_round_id, ids, shortlisted=False)
    tasks.add_task(round_invites.send_all, mails)
    return out


@router.get("/rounds/{round_id}/decisions", response_model=DecisionsOut)
async def round_decisions(
    round_id: UUID,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    """Interview / final round: every student with the AI's recommended score."""
    return await service.decisions(session, user, round_id)


@router.post("/rounds/{round_id}/decide", response_model=DecideOut)
async def decide_round(
    round_id: UUID,
    body: DecideIn,
    tasks: BackgroundTasks,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    """HR passes or fails every student; passed go on, the last round closes the drive."""
    out = await service.decide(session, user, round_id, body)
    ids = [i.candidate_id for i in out.invited]
    mails = await round_invites.for_round(session, out.next_round_id, ids, shortlisted=False)
    tasks.add_task(round_invites.send_all, mails)
    return out

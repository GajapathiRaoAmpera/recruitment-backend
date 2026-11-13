from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.advance import service
from app.advance.schemas import AdvanceIn, AdvanceOut, RankingOut
from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session

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
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    """Close this round on the cut-off and invite the passed students into the next one."""
    return await service.advance(session, user, round_id, body)

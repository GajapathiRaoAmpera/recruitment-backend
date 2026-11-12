from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import entry, repo
from app.auth.schemas import StaffUser, StepIn, VerifyIn, VerifyOut
from app.auth.security import require_role
from app.core.db import get_session
from app.core.errors import AppError

router = APIRouter(tags=["auth"])


@router.get("/me", response_model=StaffUser)
async def me(user: StaffUser = Depends(require_role("hr"))) -> StaffUser:
    return user


@router.post("/entry/verify", response_model=VerifyOut)
async def entry_verify(
    body: VerifyIn, request: Request, session: AsyncSession = Depends(get_session)
) -> VerifyOut:
    meta = {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }
    return await entry.verify(session, body, meta)


@router.post("/entry/steps", status_code=204)
async def entry_step(
    body: StepIn,
    who: entry.Entry = Depends(entry.current_entry),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if await repo.get_invite_target(session, who.candidate_id, body.round_id) is None:
        raise AppError(403, "NOT_INVITED", "You are not invited to this round")
    await repo.log_step(session, who.candidate_id, body.round_id, body.step, body.ok, body.meta)
    await session.commit()
    return Response(status_code=204)

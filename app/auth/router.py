from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import entry, repo, staff
from app.auth.schemas import (
    ConfirmIn,
    ForgotIn,
    LoginIn,
    LoginOut,
    SetPasswordIn,
    StaffUser,
    StepIn,
    VerifyIn,
    VerifyOut,
)
from app.auth.security import require_role
from app.core.db import get_session
from app.core.errors import AppError

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=LoginOut)
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)) -> LoginOut:
    return await staff.login(session, body)


@router.post("/auth/forgot-password", status_code=204)
async def forgot_password(
    body: ForgotIn, tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)
) -> Response:
    # Always 204, sent in the background: neither the answer nor its timing says if the
    # account exists. shortcut: no per-address rate limit; add one if inboxes get spammed.
    mail = await staff.reset_link_for(session, body.email)
    if mail:
        tasks.add_task(staff.send_quietly, *mail)
    return Response(status_code=204)


@router.post("/auth/set-password", status_code=204)
async def set_password(
    body: SetPasswordIn, session: AsyncSession = Depends(get_session)
) -> Response:
    await staff.set_password(session, body)
    return Response(status_code=204)


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


@router.get("/entry/steps")
async def entry_steps(
    round_id: UUID,
    who: entry.Entry = Depends(entry.current_entry),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Latest result per step for this round, so a refresh keeps the student's progress."""
    if await repo.get_invite_target(session, who.candidate_id, round_id) is None:
        raise AppError(403, "NOT_INVITED", "You are not invited to this round")
    return await repo.latest_steps(session, who.candidate_id, round_id)


@router.post("/entry/confirm", status_code=204)
async def entry_confirm(
    body: ConfirmIn,
    request: Request,
    who: entry.Entry = Depends(entry.current_entry),
    session: AsyncSession = Depends(get_session),
) -> Response:
    meta = {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }
    await entry.confirm(session, who, body, meta)
    return Response(status_code=204)

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session
from app.core.errors import AppError
from app.interviews import frames, service
from app.interviews.schemas import (
    EvaluateOut,
    InterviewDetail,
    InterviewRow,
    NotesIn,
    SessionOut,
    StartIn,
    StartOut,
    TranscriptIn,
    TranscriptOut,
)
from app.interviews.tokens import InterviewAuth, current_interview

router = APIRouter(tags=["interviews"])


# ---------------------------------------------------------------- student (interview token)


@router.get("/interview/ping", status_code=204)
async def interview_ping():
    """Public, no DB: the interview page times it to judge the student's network."""
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post("/interview/start", response_model=StartOut)
async def interview_start(
    body: StartIn, request: Request, session: AsyncSession = Depends(get_session)
):
    """Student ID + DOB -> a token for this student's AI interview. Standalone: no invite, entry
    steps or earlier round needed. Same 401 for unknown ID and wrong DOB; 5 misses lock 15 min."""
    meta = {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "via": "interview",
    }
    return await service.start(session, body, meta)


@router.post("/interview/session", response_model=SessionOut)
async def interview_session(
    who: InterviewAuth = Depends(current_interview), session: AsyncSession = Depends(get_session)
):
    """Starts the clock (first call only) and mints a one-use Gemini Live token. Call again to
    rejoin after a reload; the transcript keeps appending."""
    return await service.open_session(session, who)


@router.post("/interview/transcript", response_model=TranscriptOut)
async def interview_transcript(
    body: TranscriptIn,
    background: BackgroundTasks,
    who: InterviewAuth = Depends(current_interview),
    session: AsyncSession = Depends(get_session),
):
    return await service.save_transcript(session, who, body, background)


@router.post("/interview/frame", status_code=204)
async def interview_frame(
    request: Request,
    who: InterviewAuth = Depends(current_interview),
    session: AsyncSession = Depends(get_session),
):
    """Body: one JPEG (max 100 KB). Kept in memory for HR's live view, never stored."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > frames.MAX_BYTES:
        raise AppError(413, "FRAME_TOO_LARGE", "Camera image is too large")
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > frames.MAX_BYTES:
            raise AppError(413, "FRAME_TOO_LARGE", "Camera image is too large")
    await service.save_frame(session, who, data)
    return Response(status_code=204)


# --------------------------------------------------------------------------------------- HR


@router.get("/interviews", response_model=list[InterviewRow])
async def list_interviews(
    round_id: UUID | None = None,
    drive_id: UUID | None = None,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    return await service.list_interviews(session, user, round_id, drive_id)


@router.get("/interviews/{interview_id}", response_model=InterviewDetail)
async def get_interview(
    interview_id: UUID,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    return await service.detail(session, user, interview_id)


@router.get("/interviews/{interview_id}/frame")
async def get_frame(
    interview_id: UUID,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    """Latest camera still if under 10 s old, else 404 NO_FRAME."""
    data = await service.frame(session, user, interview_id)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/interviews/{interview_id}/evaluate", response_model=EvaluateOut, status_code=202)
async def evaluate(
    interview_id: UUID,
    background: BackgroundTasks,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    return await service.evaluate(session, user, interview_id, background)


@router.patch("/interviews/{interview_id}", response_model=InterviewDetail)
async def patch_interview(
    interview_id: UUID,
    body: NotesIn,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    return await service.set_notes(session, user, interview_id, body.hr_notes)

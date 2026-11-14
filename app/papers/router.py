from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session
from app.papers import service
from app.papers.schemas import (
    AiStatus,
    DraftCreate,
    DraftDetail,
    DraftOut,
    QuestionEdit,
    SelectionUpdate,
    SendRequest,
    SendResult,
)

router = APIRouter(prefix="/papers", tags=["papers"])
hr = require_role("hr")  # Super Admin passes too


@router.get("/ai-status", response_model=AiStatus)
async def ai_status(_: StaffUser = Depends(hr)):
    return service.ai_status()


@router.post("/drafts", response_model=DraftOut, status_code=201)
async def create_draft(
    body: DraftCreate,
    background: BackgroundTasks,
    actor: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.create_draft(
        session, actor, background, body.drive_ref, body.title, body.settings
    )


@router.get("/drafts", response_model=list[DraftOut])
async def list_drafts(actor: StaffUser = Depends(hr), session: AsyncSession = Depends(get_session)):
    return await service.list_drafts(session, actor)


@router.get("/drafts/{draft_id}", response_model=DraftDetail)
async def get_draft(
    draft_id: UUID, actor: StaffUser = Depends(hr), session: AsyncSession = Depends(get_session)
):
    return await service.get_draft(session, actor, draft_id)


@router.patch("/drafts/{draft_id}/questions/{question_id}", response_model=DraftDetail)
async def edit_question(
    draft_id: UUID,
    question_id: str,
    body: QuestionEdit,
    actor: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.edit_question(session, actor, draft_id, question_id, body)


@router.put("/drafts/{draft_id}/selection", response_model=DraftDetail)
async def set_selection(
    draft_id: UUID,
    body: SelectionUpdate,
    actor: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.set_selection(session, actor, draft_id, body.selected_ids)


@router.post("/drafts/{draft_id}/accept", response_model=DraftOut)
async def accept_draft(
    draft_id: UUID, actor: StaffUser = Depends(hr), session: AsyncSession = Depends(get_session)
):
    return await service.accept_draft(session, actor, draft_id)


@router.post("/drafts/{draft_id}/send", response_model=SendResult)
async def send_papers(
    draft_id: UUID,
    body: SendRequest,
    background: BackgroundTasks,
    actor: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.send_papers(session, actor, draft_id, body.candidate_refs, background)

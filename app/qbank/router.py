"""Question banks (uploaded MCQ PDFs): /api/v1/question-banks. HR and Super Admin only."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session
from app.papers.schemas import Level, QuestionEdit, Track
from app.qbank import service
from app.qbank.schemas import (
    MAX_PAGE_SIZE,
    NAME_MAX,
    NOTES_MAX,
    ApprovalResult,
    ApprovalUpdate,
    Availability,
    BankQuestionOut,
    QuestionPage,
    SourceDetail,
    SourceOut,
    SourcePage,
    SourcePatch,
)

router = APIRouter(prefix="/question-banks", tags=["question-banks"])
hr = require_role("hr")  # Super Admin passes too


@router.post("", response_model=SourceOut, status_code=201)
async def upload_bank(
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="The question-bank PDF")],
    name: Annotated[str, Form(min_length=1, max_length=NAME_MAX)],
    track: Annotated[Track, Form()],
    notes: Annotated[str | None, Form(max_length=NOTES_MAX)] = None,
    actor: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    """Parse the PDF and store it with its questions (unapproved). Embeddings follow in the
    background (embedding_status 'pending' -> 'done' | 'failed' | 'skipped')."""
    data = await service.read_upload(file)
    return await service.create_source(
        session,
        actor,
        background,
        data=data,
        filename=file.filename,
        name=name,
        track=track,
        notes=notes,
    )


@router.get("", response_model=SourcePage)
async def list_banks(
    track: Track | None = None,
    is_active: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    actor: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.list_sources(
        session, actor, track=track, is_active=is_active, page=page, page_size=page_size
    )


# declared before /{source_id} so "availability" is never read as an id
@router.get("/availability", response_model=Availability)
async def availability(
    track: Track,
    _: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    """Approved questions per subject and level across active, ready banks of a track."""
    return await service.availability(session, track)


@router.get("/{source_id}", response_model=SourceDetail)
async def get_bank(
    source_id: UUID, actor: StaffUser = Depends(hr), session: AsyncSession = Depends(get_session)
):
    return await service.get_source(session, actor, source_id)


@router.get("/{source_id}/questions", response_model=QuestionPage)
async def list_questions(
    source_id: UUID,
    subject: str | None = Query(None, max_length=NAME_MAX),
    level: Level | None = None,
    approved: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    _: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.list_questions(
        session,
        source_id,
        subject=subject,
        level=level,
        approved=approved,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{source_id}/file",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def download_bank(
    source_id: UUID, _: StaffUser = Depends(hr), session: AsyncSession = Depends(get_session)
):
    """The original PDF as a download (behind the HR login: fetch with the bearer token)."""
    data, filename = await service.download(session, source_id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.patch("/{source_id}", response_model=SourceDetail)
async def update_bank(
    source_id: UUID,
    body: SourcePatch,
    actor: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.update_source(session, actor, source_id, body)


@router.put("/{source_id}/file", response_model=SourceDetail)
async def replace_bank_file(
    source_id: UUID,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="The new question-bank PDF")],
    actor: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    """Re-parse and replace every question of the bank (approval and embeddings reset)."""
    data = await service.read_upload(file)
    return await service.replace_file(
        session, actor, background, source_id, data=data, filename=file.filename
    )


@router.delete("/{source_id}", status_code=204, response_class=Response)
async def delete_bank(
    source_id: UUID, actor: StaffUser = Depends(hr), session: AsyncSession = Depends(get_session)
):
    await service.delete_source(session, actor, source_id)
    return Response(status_code=204)


@router.patch("/{source_id}/questions/{question_id}", response_model=BankQuestionOut)
async def edit_question(
    source_id: UUID,
    question_id: UUID,
    body: QuestionEdit,
    actor: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.edit_question(session, actor, source_id, question_id, body)


@router.put("/{source_id}/approval", response_model=ApprovalResult)
async def set_approval(
    source_id: UUID,
    body: ApprovalUpdate,
    actor: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.set_approval(session, actor, source_id, body)

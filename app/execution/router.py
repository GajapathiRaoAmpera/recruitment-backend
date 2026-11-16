from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.execution import service
from app.execution.deps import AttemptContext, judge0_client, student_attempt
from app.execution.judge0 import Judge0Client
from app.execution.schemas import CodeRequest, DraftIn, DraftsOut, RunOut, SubmitCodeOut

router = APIRouter(prefix="/attempt", tags=["execution"])


@router.post("/run", response_model=RunOut)
async def run(
    body: CodeRequest,
    ctx: AttemptContext = Depends(student_attempt),
    session: AsyncSession = Depends(get_session),
    client: Judge0Client = Depends(judge0_client),
):
    return await service.run(session, client, ctx, body)


@router.post("/submit-code", response_model=SubmitCodeOut)
async def submit_code(
    body: CodeRequest,
    ctx: AttemptContext = Depends(student_attempt),
    session: AsyncSession = Depends(get_session),
    client: Judge0Client = Depends(judge0_client),
):
    return await service.submit_code(session, client, ctx, body)


@router.put("/code-drafts/{question_id}", status_code=204)
async def save_draft(
    question_id: UUID,
    body: DraftIn,
    ctx: AttemptContext = Depends(student_attempt),
    session: AsyncSession = Depends(get_session),
):
    await service.save_draft(session, ctx, question_id, body)
    return Response(status_code=204)


@router.get("/code-drafts", response_model=DraftsOut)
async def get_drafts(
    ctx: AttemptContext = Depends(student_attempt),
    session: AsyncSession = Depends(get_session),
):
    return await service.drafts(session, ctx)

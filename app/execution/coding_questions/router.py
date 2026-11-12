from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session
from app.core.errors import AppError
from app.execution.coding_questions import service
from app.execution.coding_questions.schemas import (
    CodingQuestionIn,
    CodingQuestionOut,
    CodingQuestionPatch,
    CodingQuestionSummary,
)

# Only the coding part of /questions lives here. MCQ questions are the question-bank /
# paper-builder code's job: they arrive through /question-banks and /papers.
router = APIRouter(prefix="/questions", tags=["questions"])
hr = require_role("hr")  # Super Admin passes too


@router.post("", response_model=CodingQuestionOut, status_code=201)
async def create_question(
    body: CodingQuestionIn,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.create(session, user, body)


@router.get("", response_model=list[CodingQuestionSummary])
async def list_questions(
    kind: str = "coding",
    difficulty: str | None = Query(default=None, pattern="^(easy|medium|advanced)$"),
    tags: list[str] | None = Query(default=None),
    approved: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    if kind != "coding":
        raise AppError(422, "UNSUPPORTED_KIND", "Only kind=coding is listed here")
    return await service.list_(session, user, difficulty, tags, approved, limit)


@router.get("/{question_id}", response_model=CodingQuestionOut)
async def get_question(
    question_id: UUID,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.get(session, user, question_id)


@router.patch("/{question_id}", response_model=CodingQuestionOut)
async def update_question(
    question_id: UUID,
    body: CodingQuestionPatch,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.update(session, user, question_id, body)

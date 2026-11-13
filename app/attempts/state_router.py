from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.attempts import state_service as service
from app.attempts.state_schemas import AnswerIn, AttemptOut, SubmitOut
from app.core.db import get_session
from app.execution.deps import AttemptContext, student_attempt

router = APIRouter(tags=["attempt"])


@router.get("/attempt", response_model=AttemptOut)
async def get_attempt(
    ctx: AttemptContext = Depends(student_attempt), session: AsyncSession = Depends(get_session)
):
    return await service.state(session, ctx.attempt_id, ctx.candidate_id)


@router.put("/attempt/answers/{question_id}", status_code=204)
async def put_answer(
    question_id: UUID,
    body: AnswerIn,
    ctx: AttemptContext = Depends(student_attempt),
    session: AsyncSession = Depends(get_session),
):
    await service.answer(session, ctx.attempt_id, question_id, body.response)
    return Response(status_code=204)


@router.post("/attempt/submit", response_model=SubmitOut)
async def submit_attempt(
    ctx: AttemptContext = Depends(student_attempt), session: AsyncSession = Depends(get_session)
):
    return await service.submit(session, ctx.attempt_id)

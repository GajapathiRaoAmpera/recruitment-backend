from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session
from app.execution.test_config import service
from app.execution.test_config.schemas import (
    RoundTestOut,
    TestConfigIn,
    TestConfigOut,
    TestQuestionsIn,
    TestQuestionsOut,
)

router = APIRouter(tags=["tests"])
hr = require_role("hr")  # Super Admin passes too


@router.get("/rounds/{round_id}/test", response_model=RoundTestOut)
async def read_test(
    round_id: UUID,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    """The round's test (its `id` is the test id) with the question order and the lock."""
    return await service.get_config(session, user, round_id)


@router.put("/rounds/{round_id}/test", response_model=TestConfigOut)
async def configure_test(
    round_id: UUID,
    body: TestConfigIn,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.configure(session, user, round_id, body)


@router.put("/tests/{test_id}/questions", response_model=TestQuestionsOut)
async def set_test_questions(
    test_id: UUID,
    body: TestQuestionsIn,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.set_questions(session, user, test_id, body.question_ids)

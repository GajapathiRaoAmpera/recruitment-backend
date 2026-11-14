from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session
from app.execution.results import service
from app.execution.results.schemas import AttemptSubmissions, TestResults

router = APIRouter(prefix="/tests", tags=["results"])
hr = require_role("hr")  # Super Admin passes too


@router.get("/{test_id}/results", response_model=TestResults)
async def test_results(
    test_id: UUID,
    limit: int = Query(default=500, ge=1, le=service.MAX_ROWS),
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.results(session, user, test_id, limit)


@router.get("/{test_id}/results/{attempt_id}", response_model=AttemptSubmissions)
async def attempt_submissions(
    test_id: UUID,
    attempt_id: UUID,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    return await service.submissions(session, user, test_id, attempt_id)

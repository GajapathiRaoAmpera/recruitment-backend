from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import repo, service
from app.admin.schemas import (
    AuditLogPage,
    AuditSummary,
    UserCreate,
    UserCreated,
    UserOut,
    UserPage,
    UserPatch,
)
from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session
from app.core.errors import AppError

router = APIRouter(prefix="/admin", tags=["admin"])
super_admin = require_role("super_admin")


@router.get("/users", response_model=UserPage)
async def list_users(
    _: StaffUser = Depends(super_admin), session: AsyncSession = Depends(get_session)
):
    # shortcut: staff list is small (tens), so no pagination yet; next_cursor stays null
    return {"items": await repo.list_users(session), "next_cursor": None}


@router.post("/users", response_model=UserCreated, status_code=201)
async def create_user(
    body: UserCreate,
    actor: StaffUser = Depends(super_admin),
    session: AsyncSession = Depends(get_session),
):
    return await service.create_user(session, actor, body)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: UUID,
    body: UserPatch,
    actor: StaffUser = Depends(super_admin),
    session: AsyncSession = Depends(get_session),
):
    return await service.update_user(session, actor, user_id, body)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    actor: StaffUser = Depends(super_admin),
    session: AsyncSession = Depends(get_session),
):
    await service.delete_user(session, actor, user_id)


@router.get("/audit-logs", response_model=AuditLogPage)
async def list_audit_logs(
    actor_id: UUID | None = None,
    action: str | None = Query(None, max_length=100),
    q: str | None = Query(None, max_length=200),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    _: StaffUser = Depends(super_admin),
    session: AsyncSession = Depends(get_session),
):
    """Read only, so not audited. cursor = the last id of the previous page."""
    try:
        before_id = int(cursor) if cursor else None
    except ValueError as e:
        raise AppError(422, "BAD_CURSOR", "Invalid cursor") from e
    filters = dict(
        actor_id=actor_id,
        action=action or None,
        q=(q or "").strip() or None,
        since=since,
        until=until,
    )
    rows = await repo.list_audit_logs(session, **filters, before_id=before_id, limit=limit)
    total = await repo.count_audit_logs(session, **filters)
    more = len(rows) > limit
    items = rows[:limit]
    return {
        "items": items,
        "next_cursor": str(items[-1]["id"]) if more else None,
        "total": total,
    }


@router.get("/audit-logs/summary", response_model=AuditSummary)
async def audit_summary(
    _: StaffUser = Depends(super_admin), session: AsyncSession = Depends(get_session)
):
    return await repo.audit_summary(session)


@router.get("/audit-logs/actions", response_model=list[str])
async def list_audit_actions(
    _: StaffUser = Depends(super_admin), session: AsyncSession = Depends(get_session)
):
    return await repo.list_audit_actions(session)

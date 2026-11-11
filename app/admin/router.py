from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import repo, service
from app.admin.schemas import UserCreate, UserOut, UserPage, UserPatch
from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session

router = APIRouter(prefix="/admin", tags=["admin"])
super_admin = require_role("super_admin")


@router.get("/users", response_model=UserPage)
async def list_users(
    _: StaffUser = Depends(super_admin), session: AsyncSession = Depends(get_session)
):
    # shortcut: staff list is small (tens), so no pagination yet; next_cursor stays null
    return {"items": await repo.list_users(session), "next_cursor": None}


@router.post("/users", response_model=UserOut, status_code=201)
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

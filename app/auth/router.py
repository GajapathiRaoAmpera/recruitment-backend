from fastapi import APIRouter, Depends

from app.auth.schemas import StaffUser
from app.auth.security import require_role

router = APIRouter(tags=["auth"])


@router.get("/me", response_model=StaffUser)
async def me(user: StaffUser = Depends(require_role("hr"))) -> StaffUser:
    return user

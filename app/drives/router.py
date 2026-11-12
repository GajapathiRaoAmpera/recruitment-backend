from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.audit import audit
from app.core.db import get_session

router = APIRouter(prefix="/drives", tags=["drives"])


class DriveIn(BaseModel):
    company: str = Field(min_length=1)
    campus: str = Field(min_length=1)
    role_title: str = Field(min_length=1)
    jd_text: str = Field(min_length=1)


class DriveOut(DriveIn):
    id: UUID
    status: str


@router.post("", response_model=DriveOut, status_code=201)
async def create_drive(
    body: DriveIn,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    # shortcut: create only; list/get/patch and rounds come with the HR data-connection work
    result = await session.execute(
        text(
            "insert into drives (owner_id, company, campus, role_title, jd_text) "
            "values (:o, :company, :campus, :role_title, :jd_text) "
            "returning id, company, campus, role_title, jd_text, status"
        ),
        {"o": user.id, **body.model_dump()},
    )
    row = result.mappings().one()
    await audit(session, user.id, "drive.create", "drive", row["id"])
    await session.commit()
    return row

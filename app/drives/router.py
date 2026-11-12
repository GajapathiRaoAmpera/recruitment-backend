from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.audit import audit
from app.core.db import get_session
from app.core.errors import AppError
from app.drives import rounds as rounds_mod
from app.drives.rounds import RoundIn, check_rounds

router = APIRouter(prefix="/drives", tags=["drives"])


class DriveIn(BaseModel):
    company: str = Field(min_length=1)
    campus: str = Field(min_length=1)
    role_title: str = Field(min_length=1)
    jd_text: str = Field(min_length=1)


class DriveCreate(DriveIn):
    rounds: list[RoundIn] | None = (
        None  # omitted = shortlist, aptitude, coding, AI interview, final
    )


class DriveOut(DriveIn):
    id: UUID
    status: str
    candidates: int = 0


@router.post("", response_model=DriveOut, status_code=201)
async def create_drive(
    body: DriveCreate,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    # shortcut: no patch; extra rounds can't be added after creation yet (HR data-connection work)
    try:
        chosen = check_rounds(body.rounds) if body.rounds is not None else rounds_mod.DEFAULT_ROUNDS
    except ValueError as e:
        raise AppError(422, "INVALID_ROUNDS", str(e)) from e
    result = await session.execute(
        text(
            "insert into drives (owner_id, company, campus, role_title, jd_text) "
            "values (:o, :company, :campus, :role_title, :jd_text) "
            "returning id, company, campus, role_title, jd_text, status, 0 as candidates"
        ),
        {"o": user.id, **body.model_dump(exclude={"rounds"})},
    )
    row = result.mappings().one()
    await rounds_mod.create_rounds(session, row["id"], chosen)
    await audit(
        session, user.id, "drive.create", "drive", row["id"], {"rounds": [r.type for r in chosen]}
    )
    await session.commit()
    return row


_SELECT = (
    "select d.id, d.company, d.campus, d.role_title, d.jd_text, d.status, "
    "(select count(*) from candidates c where c.drive_id = d.id) as candidates from drives d "
)


@router.get("", response_model=list[DriveOut])
async def list_drives(
    user: StaffUser = Depends(require_role("hr")), session: AsyncSession = Depends(get_session)
):
    """HR sees their own drives, Super Admin sees all. Newest first."""
    owner = None if user.role == "super_admin" else user.id
    result = await session.execute(
        text(
            _SELECT
            + "where (cast(:o as uuid) is null or d.owner_id = :o) order by d.created_at desc"
        ),
        {"o": owner},
    )
    return list(result.mappings())


@router.get("/{drive_id}", response_model=DriveOut)
async def get_drive(
    drive_id: UUID,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    owner = None if user.role == "super_admin" else user.id
    result = await session.execute(
        text(_SELECT + "where d.id = :id and (cast(:o as uuid) is null or d.owner_id = :o)"),
        {"id": drive_id, "o": owner},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise AppError(404, "DRIVE_NOT_FOUND", "Drive not found")
    return row

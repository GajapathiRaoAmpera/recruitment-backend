import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
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
    # Super Admin only: the HR who runs (owns) the drive. Omitted = the caller owns it.
    owner_id: UUID | None = None


class OwnerIn(BaseModel):
    owner_id: UUID


class RoundBrief(BaseModel):
    id: UUID
    type: str
    status: str  # pending | active | skipped | completed


class DriveOut(DriveIn):
    id: UUID
    status: str
    owner_id: UUID | None = None
    owner_name: str | None = None
    candidates: int = 0
    rounds: list[RoundBrief] = []  # pipeline order; empty on create
    current_round: RoundBrief | None = None  # furthest active round, None if none is active

    @model_validator(mode="before")
    @classmethod
    def _fill_rounds(cls, data):
        # the query aggregates rounds as JSON (a string with raw text() on asyncpg)
        if not isinstance(data, dict) and not hasattr(data, "keys"):
            return data
        data = dict(data)
        rounds = data.get("rounds") or []
        if isinstance(rounds, str):
            rounds = json.loads(rounds)
        data["rounds"] = rounds
        if data.get("current_round") is None:
            active = [r for r in rounds if r["status"] == "active"]
            data["current_round"] = active[-1] if active else None
        return data


async def _check_owner(session: AsyncSession, owner_id: UUID) -> str:
    """The new owner must be an active HR user (HR is who runs drives). Returns their name."""
    result = await session.execute(
        text("select full_name, role::text as role, is_active from users where id = :id"),
        {"id": owner_id},
    )
    owner = result.mappings().one_or_none()
    if owner is None or owner["role"] != "hr" or not owner["is_active"]:
        raise AppError(422, "INVALID_OWNER", "Choose an active HR user to run this drive")
    return owner["full_name"]


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
    # HR always owns what they create; only a Super Admin may hand a new drive to an HR (AC-1 keeps
    # HR limited to their own drives, so a Super-Admin drive nobody owns would be invisible to HR).
    owner_id, owner_name = user.id, user.full_name
    if body.owner_id is not None and body.owner_id != user.id:
        if user.role != "super_admin":
            raise AppError(
                403, "FORBIDDEN", "Only a Super Admin can create a drive for someone else"
            )
        owner_id, owner_name = body.owner_id, await _check_owner(session, body.owner_id)
    result = await session.execute(
        text(
            "insert into drives (owner_id, company, campus, role_title, jd_text) "
            "values (:o, :company, :campus, :role_title, :jd_text) "
            "returning id, company, campus, role_title, jd_text, status, owner_id, 0 as candidates"
        ),
        {"o": owner_id, **body.model_dump(exclude={"rounds", "owner_id"})},
    )
    row = dict(result.mappings().one())
    row["owner_name"] = owner_name
    await rounds_mod.create_rounds(session, row["id"], chosen)
    diff: dict = {"rounds": [r.type for r in chosen]}
    if owner_id != user.id:
        diff["owner_id"] = str(owner_id)
    await audit(session, user.id, "drive.create", "drive", row["id"], diff)
    await session.commit()
    return row


_SELECT = (
    "select d.id, d.company, d.campus, d.role_title, d.jd_text, d.status, d.owner_id, "
    "(select u.full_name from users u where u.id = d.owner_id) as owner_name, "
    "(select count(*) from candidates c where c.drive_id = d.id) as candidates, "
    "coalesce((select json_agg(json_build_object('id', r.id, 'type', r.type, "
    "'status', r.status) order by r.position) from rounds r where r.drive_id = d.id), "
    "'[]'::json) as rounds from drives d "
)


@router.get("", response_model=list[DriveOut])
async def list_drives(
    status: Literal["draft", "active", "closed"] | None = None,
    limit: int | None = Query(None, ge=1, le=200),
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    """HR sees their own drives, Super Admin sees all. Newest first. Optional ?status=, ?limit=."""
    owner = None if user.role == "super_admin" else user.id
    result = await session.execute(
        text(
            _SELECT + "where (cast(:o as uuid) is null or d.owner_id = :o) "
            "and (cast(:s as text) is null or d.status::text = :s) "
            "order by d.created_at desc limit :lim"
        ),
        {"o": owner, "s": status, "lim": limit},
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


@router.patch("/{drive_id}/owner", response_model=DriveOut)
async def reassign_drive(
    drive_id: UUID,
    body: OwnerIn,
    user: StaffUser = Depends(require_role("super_admin")),
    session: AsyncSession = Depends(get_session),
):
    """Super Admin hands a drive to another HR; it moves from the old owner's portal to theirs."""
    await _check_owner(session, body.owner_id)
    result = await session.execute(
        text(
            "update drives d set owner_id = :o from drives old "
            "where d.id = :id and old.id = d.id returning old.owner_id as previous"
        ),
        {"o": body.owner_id, "id": drive_id},
    )
    moved = result.mappings().one_or_none()
    if moved is None:
        raise AppError(404, "DRIVE_NOT_FOUND", "Drive not found")
    await audit(
        session,
        user.id,
        "drive.reassign",
        "drive",
        drive_id,
        {"from": str(moved["previous"]), "to": str(body.owner_id)},
    )
    await session.commit()
    result = await session.execute(text(_SELECT + "where d.id = :id"), {"id": drive_id})
    return result.mappings().one()

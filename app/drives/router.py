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


class RoundBrief(BaseModel):
    id: UUID
    type: str
    status: str  # pending | active | skipped | completed


class DriveOut(DriveIn):
    id: UUID
    status: str
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

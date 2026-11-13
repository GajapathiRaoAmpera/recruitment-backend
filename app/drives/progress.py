"""HR view of one round: who finished each entry check, who is mid-test, who submitted."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session
from app.core.errors import AppError

router = APIRouter(prefix="/drives", tags=["drives"])

STEPS = ("id_dob", "location", "terms", "fullscreen")  # fixed list: safe to put in the SQL


class ProgressRow(BaseModel):
    candidate_id: UUID
    student_id: str
    full_name: str
    status: str  # round_candidates.status
    steps: dict[str, bool | None]  # newest log per step; None = never tried
    attempt_status: str | None  # in_progress | submitted | locked | expired, None = not started
    started_at: datetime | None
    submitted_at: datetime | None
    marks: float | None
    tab_switches: int


class RoundProgress(BaseModel):
    round_id: UUID
    type: str
    status: str
    candidates: list[ProgressRow]


_STEP_COLS = ", ".join(
    f"(select vl.ok from verification_logs vl where vl.candidate_id = c.id "
    f"and vl.round_id = rc.round_id and vl.step = '{s}' "
    f"order by vl.created_at desc limit 1) as step_{s}"
    for s in STEPS
)
_ROWS = (
    "select c.id as candidate_id, c.student_id, c.full_name, rc.status, "
    + _STEP_COLS
    + ", att.status as attempt_status, att.started_at, att.submitted_at, att.marks, "
    "coalesce((select count(*) from violations v where v.attempt_id = att.id "
    "and v.kind = 'tab_switch'), 0) as tab_switches "
    "from round_candidates rc join candidates c on c.id = rc.candidate_id "
    "left join lateral (select a.id, a.status, a.started_at, a.submitted_at, a.marks "
    "from attempts a join tests t on t.id = a.test_id "
    "where t.round_id = rc.round_id and a.candidate_id = c.id "
    "order by a.started_at desc limit 1) att on true "
    "where rc.round_id = :r order by c.full_name"
)


@router.get("/{drive_id}/rounds/{round_id}/progress", response_model=RoundProgress)
async def round_progress(
    drive_id: UUID,
    round_id: UUID,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
) -> RoundProgress:
    owner = None if user.role == "super_admin" else user.id  # HR sees only their own drives
    found = await session.execute(
        text(
            "select r.id, r.type, r.status from rounds r join drives d on d.id = r.drive_id "
            "where r.id = :r and r.drive_id = :d "
            "and (cast(:o as uuid) is null or d.owner_id = :o)"
        ),
        {"r": round_id, "d": drive_id, "o": owner},
    )
    rnd = found.mappings().one_or_none()
    if rnd is None:
        raise AppError(404, "ROUND_NOT_FOUND", "Round not found")
    rows = (await session.execute(text(_ROWS), {"r": round_id})).mappings()
    return RoundProgress(
        round_id=rnd["id"],
        type=rnd["type"],
        status=rnd["status"],
        candidates=[
            ProgressRow(
                **{k: v for k, v in r.items() if not k.startswith("step_")},
                steps={s: r[f"step_{s}"] for s in STEPS},
            )
            for r in rows
        ],
    )


class RoundOut(BaseModel):
    id: UUID
    position: int
    type: str
    status: str


@router.get("/{drive_id}/rounds", response_model=list[RoundOut])
async def list_rounds(
    drive_id: UUID,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
) -> list[RoundOut]:
    owner = None if user.role == "super_admin" else user.id
    result = await session.execute(
        text(
            "select r.id, r.position, r.type, r.status from rounds r "
            "join drives d on d.id = r.drive_id where r.drive_id = :d "
            "and (cast(:o as uuid) is null or d.owner_id = :o) order by r.position"
        ),
        {"d": drive_id, "o": owner},
    )
    return [RoundOut(**r) for r in result.mappings()]

"""Super Admin overview: every drive, every round, how many candidates are where."""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session

router = APIRouter(prefix="/admin", tags=["admin"])


class RoundCounts(BaseModel):
    round_id: UUID
    position: int
    type: str
    status: str
    total: int
    not_started: int
    in_progress: int
    submitted: int
    locked: int
    expired: int


class DriveProgress(BaseModel):
    drive_id: UUID
    company: str
    campus: str
    role_title: str
    rounds: list[RoundCounts]


_SQL = (
    "select d.id as drive_id, d.company, d.campus, d.role_title, r.id as round_id, r.position, "
    "r.type, r.status, count(rc.candidate_id) as total, "
    "count(*) filter (where att.status = 'in_progress') as in_progress, "
    "count(*) filter (where att.status = 'submitted') as submitted, "
    "count(*) filter (where att.status = 'locked') as locked, "
    "count(*) filter (where att.status = 'expired') as expired "
    "from drives d join rounds r on r.drive_id = d.id "
    "left join round_candidates rc on rc.round_id = r.id "
    "left join lateral (select a.status from attempts a join tests t on t.id = a.test_id "
    "where t.round_id = r.id and a.candidate_id = rc.candidate_id "
    "order by a.started_at desc limit 1) att on true "
    "group by d.id, r.id order by d.created_at desc, r.position"
)


# shortcut: no pagination, one query for all drives; fine for tens of drives, page it past ~200
@router.get("/progress", response_model=list[DriveProgress])
async def progress_overview(
    _: StaffUser = Depends(require_role("super_admin")),
    session: AsyncSession = Depends(get_session),
) -> list[DriveProgress]:
    drives: dict[UUID, DriveProgress] = {}
    for r in (await session.execute(text(_SQL))).mappings():
        d = drives.setdefault(
            r["drive_id"],
            DriveProgress(
                drive_id=r["drive_id"],
                company=r["company"],
                campus=r["campus"],
                role_title=r["role_title"],
                rounds=[],
            ),
        )
        started = r["in_progress"] + r["submitted"] + r["locked"] + r["expired"]
        d.rounds.append(
            RoundCounts(
                round_id=r["round_id"],
                position=r["position"],
                type=r["type"],
                status=r["status"],
                total=r["total"],
                not_started=r["total"] - started,
                in_progress=r["in_progress"],
                submitted=r["submitted"],
                locked=r["locked"],
                expired=r["expired"],
            )
        )
    return list(drives.values())

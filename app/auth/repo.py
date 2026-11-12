import json
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_user(session: AsyncSession, user_id: UUID) -> RowMapping | None:
    result = await session.execute(
        text("select id, email, full_name, role, is_active from users where id = :id"),
        {"id": user_id},
    )
    return result.mappings().one_or_none()


async def get_invite_target(session: AsyncSession, candidate_id: UUID, round_id: UUID):
    """The candidate + round, only if the candidate is actually invited to that round."""
    result = await session.execute(
        text(
            "select c.id as candidate_id, c.student_id, c.dob, c.full_name, "
            "r.type as round_type, t.id as test_id "
            "from candidates c "
            "join rounds r on r.id = :rid and r.drive_id = c.drive_id "
            "join round_candidates rc on rc.round_id = r.id and rc.candidate_id = c.id "
            "left join tests t on t.round_id = r.id "
            "where c.id = :cid"
        ),
        {"cid": candidate_id, "rid": round_id},
    )
    return result.mappings().one_or_none()


async def find_by_student_id(session: AsyncSession, student_id: str):
    """One row per (candidate, round) the student can sit now; newest candidate first."""
    result = await session.execute(
        text(
            "select c.id as candidate_id, c.dob, c.full_name, r.id as round_id, "
            'r.type as round_type, t.id as test_id, r."Duration" as duration_min '
            "from candidates c "
            "join round_candidates rc on rc.candidate_id = c.id "
            "and rc.status in ('invited', 'in_progress') "
            "join rounds r on r.id = rc.round_id and r.status = 'active' "
            "left join tests t on t.round_id = r.id "
            "where lower(c.student_id) = lower(:sid) "
            "and not exists (select 1 from attempts at join tests tt on tt.id = at.test_id "
            "where tt.round_id = r.id and at.candidate_id = c.id and at.status = 'submitted') "
            "order by c.created_at desc, r.position"
        ),
        {"sid": student_id.strip()},
    )
    return list(result.mappings())


async def recent_failures(session: AsyncSession, candidate_id: UUID) -> int:
    result = await session.execute(
        text(
            "select count(*) from verification_logs where candidate_id = :cid "
            "and step = 'id_dob' and not ok and created_at > now() - interval '15 minutes'"
        ),
        {"cid": candidate_id},
    )
    return result.scalar_one()


async def log_step(
    session: AsyncSession, candidate_id: UUID, round_id: UUID, step: str, ok: bool, meta: dict
) -> None:
    await session.execute(
        text(
            "insert into verification_logs (candidate_id, round_id, step, ok, meta, created_at) "
            "values (:cid, :rid, :step, :ok, cast(:meta as jsonb), clock_timestamp())"
        ),
        {"cid": candidate_id, "rid": round_id, "step": step, "ok": ok, "meta": json.dumps(meta)},
    )

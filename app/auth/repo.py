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


async def get_login(session: AsyncSession, email: str) -> RowMapping | None:
    """Everything sign-in needs, by email (case-insensitive). Never returned to clients."""
    result = await session.execute(
        text(
            "select id, email, full_name, role, is_active, password_hash, password_set_at, "
            "failed_logins, locked_until, locked_until > now() as locked "
            "from users where lower(email) = lower(:e)"
        ),
        {"e": email.strip()},
    )
    return result.mappings().one_or_none()


async def get_password_state(session: AsyncSession, user_id: UUID) -> RowMapping | None:
    result = await session.execute(
        text("select id, email, is_active, password_set_at from users where id = :id"),
        {"id": user_id},
    )
    return result.mappings().one_or_none()


async def login_failed(session: AsyncSession, user_id: UUID, max_failures: int) -> None:
    """Count a wrong password; the max-th in a row locks the account for 15 minutes."""
    await session.execute(
        text(
            "update users set failed_logins = case when failed_logins + 1 >= :max then 0 "
            "else failed_logins + 1 end, "
            "locked_until = case when failed_logins + 1 >= :max "
            "then now() + interval '15 minutes' else locked_until end "
            "where id = :id"
        ),
        {"id": user_id, "max": max_failures},
    )


async def login_ok(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        text("update users set failed_logins = 0, locked_until = null where id = :id"),
        {"id": user_id},
    )


async def set_password(session: AsyncSession, user_id: UUID, password_hash: str) -> None:
    """New hash; bumping password_set_at makes every earlier set-password link stale."""
    await session.execute(
        text(
            "update users set password_hash = :h, password_set_at = clock_timestamp(), "
            "failed_logins = 0, locked_until = null where id = :id"
        ),
        {"id": user_id, "h": password_hash},
    )


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


async def find_candidates(session: AsyncSession, student_id: str) -> list[RowMapping]:
    """Every candidate with this Student ID (one per drive), whatever their rounds; newest
    first. Sign-in must not depend on having an open round."""
    result = await session.execute(
        text(
            "select id as candidate_id, dob, full_name from candidates "
            "where lower(student_id) = lower(:sid) order by created_at desc"
        ),
        {"sid": student_id.strip()},
    )
    return list(result.mappings())


async def candidate_rounds(session: AsyncSession, candidate_id: UUID) -> list[RowMapping]:
    """The rounds still active for this candidate (round active, their row invited/in_progress),
    in pipeline order, with where their attempt stands. A submitted, locked or expired round is
    listed too; rounds HR already completed/advanced are not. in_progress past its deadline
    reads 'expired'."""
    result = await session.execute(
        text(
            'select r.id as round_id, r.type as round_type, t.id as test_id, r."Duration" '
            "as duration_min, case when a.id is null then 'not_started' "
            "when a.status = 'in_progress' and a.deadline_at < now() then 'expired' "
            "else cast(a.status as text) end as attempt_status "
            "from round_candidates rc "
            "join rounds r on r.id = rc.round_id and r.status = 'active' "
            "left join tests t on t.round_id = r.id "
            "left join attempts a on a.test_id = t.id and a.candidate_id = rc.candidate_id "
            "where rc.candidate_id = :c and rc.status in ('invited', 'in_progress') "
            "order by r.position"
        ),
        {"c": candidate_id},
    )
    return list(result.mappings())


async def failure_round(session: AsyncSession, candidate_id: UUID) -> UUID | None:
    """The round a wrong-DOB try is logged against (verification_logs needs one, FK): their
    first open round as before, else their latest round, so a candidate whose rounds are all
    finished is still counted toward the lockout. None only if they are in no round at all."""
    result = await session.execute(
        text(
            "select rc.round_id from round_candidates rc join rounds r on r.id = rc.round_id "
            "where rc.candidate_id = :c order by "
            "(r.status = 'active' and rc.status in ('invited', 'in_progress')) desc, "
            "case when r.status = 'active' and rc.status in ('invited', 'in_progress') "
            "then r.position end, r.position desc limit 1"
        ),
        {"c": candidate_id},
    )
    return result.scalar_one_or_none()


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


async def latest_steps(
    session: AsyncSession, candidate_id: UUID, round_id: UUID
) -> dict[str, bool]:
    """Newest log per step for this candidate + round (a refreshed page shows real progress)."""
    result = await session.execute(
        text(
            "select distinct on (step) step, ok from verification_logs "
            "where candidate_id = :c and round_id = :r order by step, created_at desc"
        ),
        {"c": candidate_id, "r": round_id},
    )
    return {r["step"]: r["ok"] for r in result.mappings()}

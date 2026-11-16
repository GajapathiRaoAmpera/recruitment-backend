from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession


async def tab_switch(
    session: AsyncSession, attempt_id: UUID, max_switches: int
) -> RowMapping | None:
    """Count and lock in ONE statement. None when the attempt isn't in progress (already locked,
    submitted or expired): nothing changes then."""
    result = await session.execute(
        text(
            "update attempts set tab_switches = tab_switches + 1, "
            "status = case when tab_switches + 1 >= :max then 'locked' else status end, "
            "remaining_sec_on_lock = case when tab_switches + 1 >= :max "
            "then greatest(0, extract(epoch from deadline_at - now())::int) "
            "else remaining_sec_on_lock end "
            "where id = :a and status = 'in_progress' and deadline_at > now() "
            "returning tab_switches, status"
        ),
        {"a": attempt_id, "max": max_switches},
    )
    return result.mappings().one_or_none()


async def current(session: AsyncSession, attempt_id: UUID) -> RowMapping | None:
    result = await session.execute(
        text("select tab_switches, status from attempts where id = :a"), {"a": attempt_id}
    )
    return result.mappings().one_or_none()


async def log_violation(session: AsyncSession, attempt_id: UUID, kind: str) -> None:
    await session.execute(
        text("insert into violations (attempt_id, kind) values (:a, :k)"),
        {"a": attempt_id, "k": kind},
    )


# ---- HR resume (POST /attempts/{id}/resume) --------------------------------------------------


async def find_for_resume(
    session: AsyncSession, attempt_id: UUID, owner: UUID | None
) -> RowMapping | None:
    """The attempt's round, only if this HR owns the drive (owner None = Super Admin)."""
    result = await session.execute(
        text(
            "select a.id, t.round_id from attempts a join tests t on t.id = a.test_id "
            "join rounds r on r.id = t.round_id join drives d on d.id = r.drive_id "
            "where a.id = :a and (cast(:o as uuid) is null or d.owner_id = :o)"
        ),
        {"a": attempt_id, "o": owner},
    )
    return result.mappings().one_or_none()


async def lock_for_resume(session: AsyncSession, attempt_id: UUID, round_id: UUID) -> RowMapping:
    """Row locks until commit, round first then attempt (the order advance takes them), so a
    resume and an advance of the same round run one after the other instead of interleaving."""
    rnd = await session.execute(
        text("select cast(status as text) as status from rounds where id = :r for share"),
        {"r": round_id},
    )
    att = await session.execute(
        text(
            "select cast(status as text) as status, tab_switches, remaining_sec_on_lock "
            "from attempts where id = :a for update"
        ),
        {"a": attempt_id},
    )
    return {"round_status": rnd.scalar_one(), **att.mappings().one()}


async def resume(session: AsyncSession, attempt_id: UUID) -> RowMapping | None:
    """Reopen a locked attempt in ONE statement: the time left at lock becomes the new deadline,
    the switch counter restarts at 0. Violations, answers, drafts and marks are not touched.
    None when it is not locked, has no saved time, or its round is not active."""
    result = await session.execute(
        text(
            "update attempts a set status = 'in_progress', tab_switches = 0, "
            "deadline_at = now() + make_interval(secs => a.remaining_sec_on_lock), "
            "remaining_sec_on_lock = null, resume_count = a.resume_count + 1 "
            "where a.id = :a and a.status = 'locked' and a.remaining_sec_on_lock is not null "
            "and exists (select 1 from tests t join rounds r on r.id = t.round_id "
            "where t.id = a.test_id and r.status = 'active') "
            "returning a.deadline_at, a.tab_switches"
        ),
        {"a": attempt_id},
    )
    return result.mappings().one_or_none()

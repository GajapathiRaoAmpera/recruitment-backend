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

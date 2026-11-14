from datetime import datetime
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

COLS = "id, email, full_name, role, is_active, created_at"


async def list_users(session: AsyncSession) -> list[RowMapping]:
    rows = await session.execute(text(f"select {COLS} from users order by created_at"))
    return list(rows.mappings())


async def get_user(session: AsyncSession, user_id: UUID) -> RowMapping | None:
    rows = await session.execute(text(f"select {COLS} from users where id = :id"), {"id": user_id})
    return rows.mappings().one_or_none()


async def email_taken(session: AsyncSession, email: str) -> bool:
    rows = await session.execute(
        text("select 1 from users where lower(email) = lower(:e)"), {"e": email}
    )
    return rows.first() is not None


async def insert_user(
    session: AsyncSession, user_id: UUID, email: str, full_name: str, role: str
) -> RowMapping:
    rows = await session.execute(
        text(
            "insert into users (id, email, full_name, role) "
            f"values (:id, :email, :full_name, cast(:role as user_role)) returning {COLS}"
        ),
        {"id": user_id, "email": email, "full_name": full_name, "role": role},
    )
    return rows.mappings().one()


async def update_user(session: AsyncSession, user_id: UUID, changes: dict) -> RowMapping:
    # keys come from the UserPatch schema, never from raw input
    sets = ", ".join(
        f"{k} = cast(:{k} as user_role)" if k == "role" else f"{k} = :{k}" for k in changes
    )
    rows = await session.execute(
        text(f"update users set {sets} where id = :id returning {COLS}"),
        {**changes, "id": user_id},
    )
    return rows.mappings().one()


async def delete_user(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(text("delete from users where id = :id"), {"id": user_id})


def _like(term: str) -> str:
    """ILIKE pattern for a literal substring: %, _ and \\ match themselves."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _audit_filters(
    actor_id: UUID | None,
    action: str | None,
    q: str | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[list[str], dict]:
    """WHERE fragments (fixed strings) and their bound values, shared by the list and its count."""
    where, params = [], {}
    if actor_id is not None:
        where.append("a.actor_id = :actor_id")
        params["actor_id"] = actor_id
    if action:
        where.append("a.action = :action")
        params["action"] = action
    if q:
        where.append(
            "(a.action ilike :q or a.entity ilike :q or a.entity_id ilike :q "
            "or u.full_name ilike :q or u.email ilike :q)"
        )
        params["q"] = _like(q)
    if since is not None:
        where.append("a.created_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("a.created_at < :until")
        params["until"] = until
    return where, params


def _where(parts: list[str]) -> str:
    return f"where {' and '.join(parts)} " if parts else ""


_AUDIT_FROM = "from audit_logs a left join users u on u.id = a.actor_id "


async def list_audit_logs(
    session: AsyncSession,
    *,
    actor_id: UUID | None,
    action: str | None,
    q: str | None,
    since: datetime | None,
    until: datetime | None,
    before_id: int | None,
    limit: int,
) -> list[RowMapping]:
    """Newest first (by id: identity order is insertion order). Returns up to limit + 1 rows
    so the caller can tell whether another page exists."""
    where, params = _audit_filters(actor_id, action, q, since, until)
    if before_id is not None:
        where.append("a.id < :before_id")
        params["before_id"] = before_id
    params["limit"] = limit + 1
    rows = await session.execute(
        text(
            "select a.id, a.created_at, a.actor_id, u.full_name as actor_name, "
            "u.email as actor_email, cast(u.role as text) as actor_role, a.action, a.entity, "
            f"a.entity_id, a.diff {_AUDIT_FROM}{_where(where)}order by a.id desc limit :limit"
        ),
        params,
    )
    return list(rows.mappings())


async def count_audit_logs(
    session: AsyncSession,
    *,
    actor_id: UUID | None,
    action: str | None,
    q: str | None,
    since: datetime | None,
    until: datetime | None,
) -> int:
    """How many rows match the filters (all pages)."""
    where, params = _audit_filters(actor_id, action, q, since, until)
    rows = await session.execute(text(f"select count(*) {_AUDIT_FROM}{_where(where)}"), params)
    return rows.scalar_one()


async def audit_summary(session: AsyncSession) -> RowMapping:
    rows = await session.execute(
        text(
            "select count(*) as total, "
            "count(*) filter (where created_at >= now() - interval '24 hours') as last_24h, "
            "count(distinct actor_id) as actors, "
            "count(*) filter (where action like 'user.%') as access_changes "
            "from audit_logs"
        )
    )
    return rows.mappings().one()


async def list_audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.execute(text("select distinct action from audit_logs order by action"))
    return list(rows.scalars())

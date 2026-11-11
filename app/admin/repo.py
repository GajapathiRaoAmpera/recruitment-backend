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

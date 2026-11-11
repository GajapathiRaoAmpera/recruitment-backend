from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_user(session: AsyncSession, user_id: UUID) -> RowMapping | None:
    result = await session.execute(
        text("select id, email, full_name, role, is_active from users where id = :id"),
        {"id": user_id},
    )
    return result.mappings().one_or_none()

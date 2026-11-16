from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    # statement_cache_size=0: transaction poolers (Supabase, PgBouncer) break prepared statements
    s = get_settings()
    return create_async_engine(
        s.database_url,
        pool_pre_ping=True,
        connect_args={
            "statement_cache_size": 0,
            "server_settings": {"search_path": s.db_schema},  # unqualified SQL hits our schema
        },
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed by the service layer."""
    async with async_sessionmaker(get_engine(), expire_on_commit=False)() as session:
        yield session

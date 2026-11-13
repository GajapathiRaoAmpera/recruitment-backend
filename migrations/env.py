import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from app.core.db import get_engine

# Migrations are hand-written SQL (op.execute), so no target_metadata / autogenerate.


def _run(connection: Connection) -> None:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _main() -> None:
    async with get_engine().connect() as connection:
        await connection.run_sync(_run)
    await get_engine().dispose()


if context.is_offline_mode():
    raise SystemExit("Offline mode is not supported; run against a database.")
asyncio.run(_main())

"""Bootstrap: create the first Super Admin and print their one-time set-password link.

    uv run python -m scripts.create_super_admin --email you@company.com --name "Your Name"

Uses DATABASE_URL + DB_SCHEMA from .env. Run again for an existing Super Admin to get a fresh
link (e.g. the first one expired). Never makes an existing HR user a Super Admin: do that in
the admin screen. The link is a credential: open it yourself, don't paste it into chats.
"""

import argparse
import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.admin import repo
from app.auth import staff
from app.core.audit import audit
from app.core.db import get_engine


async def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    a = p.parse_args(argv)
    async with async_sessionmaker(get_engine(), expire_on_commit=False)() as session:
        found = await session.execute(
            text("select id, role, password_set_at from users where lower(email) = lower(:e)"),
            {"e": a.email.strip()},
        )
        row = found.mappings().one_or_none()
        if row and row["role"] != "super_admin":
            sys.exit(f"{a.email} exists as {row['role']}; promote them in the admin screen.")
        if row is None:
            row = await repo.insert_user(session, a.email.strip(), a.name.strip(), "super_admin")
            # no actor yet: the new admin is recorded as creating themselves
            await audit(session, row["id"], "user.create", "users", row["id"], {"via": "script"})
            await session.commit()
            print(f"Created Super Admin {a.email}")
        link = staff.set_password_link(row["id"], row.get("password_set_at"), staff.INVITE_TTL)
    await get_engine().dispose()
    print(f"Set-password link (works once, 72 h):\n{link}")


if __name__ == "__main__":
    asyncio.run(main())

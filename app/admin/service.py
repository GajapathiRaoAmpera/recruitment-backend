from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import repo
from app.admin.schemas import UserCreate, UserCreated, UserOut, UserPatch
from app.auth import staff
from app.auth.schemas import StaffUser
from app.core import email
from app.core.audit import audit
from app.core.errors import AppError


async def create_user(session: AsyncSession, actor: StaffUser, body: UserCreate) -> UserCreated:
    if await repo.email_taken(session, body.email):
        raise AppError(409, "USER_EXISTS", "An account with this email already exists")
    row = await repo.insert_user(session, body.email, body.full_name, body.role)
    await audit(session, actor.id, "user.create", "users", row["id"], body.model_dump())
    link: str | None = staff.set_password_link(row["id"], None, staff.INVITE_TTL)
    if body.send_email:
        # before commit: if the email fails, the user isn't created and the admin can retry
        await email.send_email(row["email"], *staff.invite_email(row["full_name"], link))
        link = None
    await session.commit()
    return UserCreated.model_validate({**dict(row), "invite_link": link})


async def update_user(
    session: AsyncSession, actor: StaffUser, user_id: UUID, body: UserPatch
) -> UserOut:
    changes = body.model_dump(exclude_none=True)
    current = await repo.get_user(session, user_id)
    if current is None:
        raise AppError(404, "USER_NOT_FOUND", "User not found")
    if not changes:
        return UserOut.model_validate(dict(current))
    # A Super Admin locking themselves out could leave nobody able to manage users.
    if user_id == actor.id and ("is_active" in changes or "role" in changes):
        raise AppError(409, "CANNOT_CHANGE_SELF", "You can't deactivate or change your own role")
    row = await repo.update_user(session, user_id, changes)
    before = {k: current[k] for k in changes}
    await audit(session, actor.id, "user.update", "users", user_id, {"from": before, "to": changes})
    await session.commit()
    return UserOut.model_validate(dict(row))


async def delete_user(session: AsyncSession, actor: StaffUser, user_id: UUID) -> None:
    """Hard delete, only for users nothing points to (e.g. a mistaken invite)."""
    if user_id == actor.id:
        raise AppError(409, "CANNOT_CHANGE_SELF", "You can't delete your own account")
    current = await repo.get_user(session, user_id)
    if current is None:
        raise AppError(404, "USER_NOT_FOUND", "User not found")
    # Super Admins are deactivate-only: one click must never remove the people who manage access.
    if current["role"] == "super_admin":
        raise AppError(
            409,
            "SUPER_ADMIN_PROTECTED",
            "Super Admin accounts can't be deleted. Deactivate them instead.",
        )
    try:
        await repo.delete_user(session, user_id)
    except IntegrityError as e:  # drives, audit history or questions reference this user
        await session.rollback()
        raise AppError(
            409,
            "USER_IN_USE",
            "This user owns drives or has activity history, so they can't be deleted. "
            "Deactivate them instead.",
        ) from e
    snapshot = {"email": current["email"], "role": current["role"]}
    await audit(session, actor.id, "user.delete", "users", user_id, snapshot)
    await session.commit()

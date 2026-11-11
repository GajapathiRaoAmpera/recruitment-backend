from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import repo, supabase_admin
from app.admin.schemas import UserCreate, UserOut, UserPatch
from app.auth.schemas import StaffUser
from app.core.audit import audit
from app.core.errors import AppError


async def create_user(session: AsyncSession, actor: StaffUser, body: UserCreate) -> UserOut:
    if await repo.email_taken(session, body.email):
        raise AppError(409, "USER_EXISTS", "An account with this email already exists")
    # shortcut: if the DB insert fails after the invite, the Supabase user is orphaned;
    # re-inviting then returns USER_EXISTS. Add a cleanup call if this ever happens in practice.
    user_id = await supabase_admin.invite_user(body.email, body.full_name)
    row = await repo.insert_user(session, user_id, body.email, body.full_name, body.role)
    await audit(session, actor.id, "user.create", "users", user_id, body.model_dump())
    await session.commit()
    return UserOut.model_validate(dict(row))


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
    # Supabase last: if it fails, nothing above is committed and the user stays intact.
    await supabase_admin.delete_user(user_id)
    await session.commit()

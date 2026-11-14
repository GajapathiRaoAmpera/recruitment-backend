"""HR/Admin auth: verify our staff JWT, then load the role from `users` (never from claims)."""

import time
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import repo
from app.auth.schemas import Role, StaffUser
from app.core.config import get_settings
from app.core.db import get_session
from app.core.errors import AppError

STAFF_TTL = 8 * 3600  # one working day; sign in again after

bearer = HTTPBearer(auto_error=False)


def issue_token(typ: str, sub: UUID, ttl: int, **extra) -> str:
    claims = {"typ": typ, "sub": str(sub), "exp": int(time.time()) + ttl, **extra}
    return jwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")


def decode_token(token: str, typ: str) -> dict:
    """Claims of a valid, unexpired token of this type, else 401 (staff and set-password share
    the key, so `typ` is what stops one being used as the other)."""
    try:
        claims = jwt.decode(
            token,
            get_settings().jwt_secret,
            algorithms=["HS256"],  # pinned: never let the token pick its algorithm
            options={"require": ["exp", "sub", "typ"]},
        )
        if claims["typ"] != typ:
            raise jwt.InvalidTokenError("wrong token type")
        UUID(claims["sub"])
        return claims
    except (jwt.PyJWTError, ValueError) as e:
        raise AppError(401, "INVALID_TOKEN", "Sign in again") from e


async def verify_token(token: str) -> UUID:
    """Return the user id (sub) of a valid staff token, else raise 401."""
    return UUID(decode_token(token, "staff")["sub"])


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> StaffUser:
    if creds is None:
        raise AppError(401, "UNAUTHENTICATED", "Sign in required")
    row = await repo.get_user(session, await verify_token(creds.credentials))
    if row is None or not row["is_active"]:
        raise AppError(403, "NOT_STAFF", "This account has no access to Campus Hire")
    return StaffUser.model_validate(dict(row))


def require_role(*roles: Role):
    """Route dependency. Super Admin passes every check (it can do everything HR can)."""

    async def dep(user: StaffUser = Depends(current_user)) -> StaffUser:
        if user.role != "super_admin" and user.role not in roles:
            raise AppError(403, "FORBIDDEN", "You do not have access to this action")
        return user

    return dep

"""HR/Admin auth: verify the Supabase JWT, then load the role from `users` (never from claims)."""

import asyncio
from functools import lru_cache
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

# Asymmetric only: accepting HS256 here would allow algorithm-confusion attacks.
ALGORITHMS = ["ES256", "RS256"]

bearer = HTTPBearer(auto_error=False)


@lru_cache
def _jwks_client() -> jwt.PyJWKClient:
    url = get_settings().supabase_url.rstrip("/")
    return jwt.PyJWKClient(f"{url}/auth/v1/.well-known/jwks.json", lifespan=600)


async def _signing_key(token: str):
    # PyJWKClient uses blocking urllib on a cache miss; keep it off the event loop
    return (await asyncio.to_thread(_jwks_client().get_signing_key_from_jwt, token)).key


async def verify_token(token: str) -> UUID:
    """Return the Supabase user id (sub) of a valid token, else raise 401."""
    url = get_settings().supabase_url.rstrip("/")
    try:
        claims = jwt.decode(
            token,
            await _signing_key(token),
            algorithms=ALGORITHMS,
            audience="authenticated",
            issuer=f"{url}/auth/v1",
            options={"require": ["exp", "sub"]},
        )
        return UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError) as e:
        raise AppError(401, "INVALID_TOKEN", "Sign in again") from e


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

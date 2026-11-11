"""Supabase Auth admin calls. Uses the secret key, so this module only ever runs server-side."""

from uuid import UUID

import httpx

from app.core.config import get_settings
from app.core.errors import AppError


async def _call(method: str, path: str, **kwargs) -> httpx.Response:
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.request(
                method,
                f"{s.supabase_url.rstrip('/')}/auth/v1{path}",
                headers={"apikey": s.supabase_secret_key},
                **kwargs,
            )
    except httpx.HTTPError as e:
        raise AppError(502, "AUTH_PROVIDER_ERROR", "Couldn't reach Supabase. Try again.") from e


async def invite_user(email: str, full_name: str) -> UUID:
    """Create the Supabase Auth user and email them a link to set their password."""
    redirect = f"{get_settings().frontend_url.rstrip('/')}/set-password"
    r = await _call(
        "POST",
        "/invite",
        params={"redirect_to": redirect},
        json={"email": email, "data": {"full_name": full_name}},
    )
    if r.status_code == 422 and "email_exists" in r.text:
        raise AppError(409, "USER_EXISTS", "An account with this email already exists")
    if r.status_code == 429:
        raise AppError(
            429, "EMAIL_RATE_LIMIT", "Supabase's invite email limit was hit. Try again later."
        )
    if r.is_error:
        raise AppError(502, "AUTH_PROVIDER_ERROR", "Supabase couldn't create the account")
    return UUID(r.json()["id"])


async def delete_user(user_id: UUID) -> None:
    """Remove the Supabase Auth account, so the person can no longer sign in at all."""
    r = await _call("DELETE", f"/admin/users/{user_id}")
    if r.status_code == 404:
        return  # already gone in Supabase; our row is what matters
    if r.is_error:
        raise AppError(502, "AUTH_PROVIDER_ERROR", "Supabase couldn't delete the account")

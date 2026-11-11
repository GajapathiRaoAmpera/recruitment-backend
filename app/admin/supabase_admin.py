"""Supabase Auth admin calls. Uses the secret key, so this module only ever runs server-side."""

from uuid import UUID

import httpx

from app.core.config import get_settings
from app.core.errors import AppError


async def invite_user(email: str, full_name: str) -> UUID:
    """Create the Supabase Auth user and email them a link to set their password."""
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{s.supabase_url.rstrip('/')}/auth/v1/invite",
                params={"redirect_to": f"{s.frontend_url.rstrip('/')}/set-password"},
                headers={"apikey": s.supabase_secret_key},
                json={"email": email, "data": {"full_name": full_name}},
            )
    except httpx.HTTPError as e:
        raise AppError(502, "AUTH_PROVIDER_ERROR", "Couldn't reach Supabase. Try again.") from e
    if r.status_code == 422 and "email_exists" in r.text:
        raise AppError(409, "USER_EXISTS", "An account with this email already exists")
    if r.status_code == 429:
        raise AppError(
            429, "EMAIL_RATE_LIMIT", "Supabase's invite email limit was hit. Try again later."
        )
    if r.is_error:
        raise AppError(502, "AUTH_PROVIDER_ERROR", "Supabase couldn't create the account")
    return UUID(r.json()["id"])

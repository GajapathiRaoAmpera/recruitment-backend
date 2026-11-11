"""Supabase Auth admin calls. Uses the secret key, so this module only ever runs server-side."""

import logging
from uuid import UUID

import httpx

from app.core.config import get_settings
from app.core.errors import AppError

log = logging.getLogger(__name__)


def _provider_error(r: httpx.Response, what: str) -> AppError:
    """Keep Supabase's own reason (e.g. "Error sending invite email"): admins need it."""
    try:
        body = r.json()
        reason = body.get("msg") or body.get("message") or body.get("error_description") or ""
    except ValueError:
        reason = ""
    log.warning("Supabase %s failed: HTTP %s %s", what, r.status_code, reason)
    message = f"Supabase couldn't {what}" + (f": {reason}" if reason else "")
    return AppError(502, "AUTH_PROVIDER_ERROR", message, {"provider_status": r.status_code})


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


async def invite_user(
    email: str, full_name: str, send_email: bool = True
) -> tuple[UUID, str | None]:
    """Create the Supabase Auth user with a one-time link to /set-password.

    send_email=True: Supabase emails the link. False: no email; the link is returned so the
    Super Admin can share it (works without any SMTP setup). Returns (user id, link or None).
    """
    redirect = f"{get_settings().frontend_url.rstrip('/')}/set-password"
    data = {"full_name": full_name}
    if send_email:
        r = await _call(
            "POST", "/invite", params={"redirect_to": redirect}, json={"email": email, "data": data}
        )
    else:
        r = await _call(
            "POST",
            "/admin/generate_link",
            json={"type": "invite", "email": email, "data": data, "redirect_to": redirect},
        )
    if r.status_code == 422 and "email_exists" in r.text:
        raise AppError(409, "USER_EXISTS", "An account with this email already exists")
    if r.status_code == 429:
        raise AppError(
            429, "EMAIL_RATE_LIMIT", "Supabase's invite email limit was hit. Try again later."
        )
    if r.is_error:
        raise _provider_error(r, "create the account")
    body = r.json()  # generate_link returns the user's fields plus action_link at the top level
    return UUID(body["id"]), (None if send_email else body["action_link"])


async def delete_user(user_id: UUID) -> None:
    """Remove the Supabase Auth account, so the person can no longer sign in at all."""
    r = await _call("DELETE", f"/admin/users/{user_id}")
    if r.status_code == 404:
        return  # already gone in Supabase; our row is what matters
    if r.is_error:
        raise _provider_error(r, "delete the account")

"""Staff sign-in with our own passwords, plus one-time set-password links (invite and reset)."""

import asyncio
import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import repo
from app.auth.passwords import MIN_LENGTH, hash_password, verify_password
from app.auth.schemas import LoginIn, LoginOut, SetPasswordIn, StaffUser
from app.auth.security import STAFF_TTL, decode_token, issue_token
from app.core import email
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.mail_template import render

log = logging.getLogger(__name__)

MAX_FAILURES = 5  # wrong passwords in a row -> locked 15 min
INVITE_TTL = 72 * 3600  # new staff may not read mail the same day
RESET_TTL = 3600


def set_password_link(user_id: UUID, password_set_at: datetime | None, ttl: int) -> str:
    # pv = the password version this link may replace; any later set makes the link stale
    pv = password_set_at.isoformat() if password_set_at else ""
    token = issue_token("set_password", user_id, ttl, pv=pv)
    return f"{get_settings().frontend_url.rstrip('/')}/set-password?token={token}"


def invite_email(full_name: str, link: str) -> tuple[str, str, str]:
    """(subject, text, html)."""
    intro = "You've been added to Ampera Campus Hire. Set your password to sign in."
    note = "This link works once, for 72 hours. If you weren't expecting this, ignore this email."
    return (
        "Your Ampera Campus Hire account",
        f"Hi {full_name},\n\n{intro}\n\n{link}\n\n{note}\n",
        render(
            "Welcome to Campus Hire", f"Hi {full_name},", [intro], "Set your password", link, note
        ),
    )


def reset_email(full_name: str, link: str) -> tuple[str, str, str]:
    """(subject, text, html)."""
    intro = "We got a request to reset your Ampera Campus Hire password."
    note = "This link works once, for 1 hour. If you didn't ask for this, ignore this email."
    return (
        "Reset your Ampera Campus Hire password",
        f"Hi {full_name},\n\n{intro} Set a new one here:\n\n{link}\n\n{note}\n",
        render(
            "Reset your password", f"Hi {full_name},", [intro], "Set a new password", link, note
        ),
    )


def _bad_login() -> AppError:
    # one message for unknown email, wrong password, inactive or not-yet-set: no account probing
    return AppError(401, "INVALID_CREDENTIALS", "Email or password is incorrect")


async def login(session: AsyncSession, body: LoginIn) -> LoginOut:
    row = await repo.get_login(session, body.email)
    if row and row["locked"]:
        raise AppError(429, "TOO_MANY_ATTEMPTS", "Too many attempts. Try again in 15 minutes")
    stored = row["password_hash"] if row else None
    ok = await asyncio.to_thread(verify_password, body.password, stored)  # ~50 ms of CPU
    if not row or not ok:
        if row and stored:
            await repo.login_failed(session, row["id"], MAX_FAILURES)
            await session.commit()
        raise _bad_login()
    if not row["is_active"]:
        raise _bad_login()
    await repo.login_ok(session, row["id"])
    await session.commit()
    return LoginOut(
        access_token=issue_token("staff", row["id"], STAFF_TTL),
        expires_in=STAFF_TTL,
        user=StaffUser.model_validate(dict(row)),
    )


async def reset_link_for(session: AsyncSession, address: str) -> tuple[str, str, str, str] | None:
    """(to, subject, text, html) of a reset email, or None for an unknown/inactive address."""
    row = await repo.get_login(session, address)
    if row is None or not row["is_active"]:
        return None
    link = set_password_link(row["id"], row["password_set_at"], RESET_TTL)
    return (row["email"], *reset_email(row["full_name"], link))


async def send_quietly(to: str, subject: str, body: str, html: str | None = None) -> None:
    """Background send for forgot-password: the caller already got 204, so only log failures."""
    try:
        await email.send_email(to, subject, body, html)
    except AppError as e:
        log.warning("reset email not sent: %s", e.code)


async def set_password(session: AsyncSession, body: SetPasswordIn) -> None:
    stale = AppError(
        400, "LINK_INVALID", "This link has expired or was already used. Ask for a new one."
    )
    try:
        claims = decode_token(body.token, "set_password")
    except AppError as e:
        raise stale from e
    row = await repo.get_password_state(session, UUID(claims["sub"]))
    current = row["password_set_at"].isoformat() if row and row["password_set_at"] else ""
    if row is None or not row["is_active"] or claims.get("pv") != current:
        raise stale
    if len(body.password) < MIN_LENGTH:
        raise AppError(
            422, "WEAK_PASSWORD", f"Use at least {MIN_LENGTH} characters for your password."
        )
    hashed = await asyncio.to_thread(hash_password, body.password)
    await repo.set_password(session, row["id"], hashed)
    await audit(session, row["id"], "user.set_password", "users", row["id"])  # no secrets in diff
    await session.commit()

"""Plain-text (+ optional HTML) email over SMTP + STARTTLS.

Gmail: smtp.gmail.com:587 with an App Password."""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import get_settings
from app.core.errors import AppError

log = logging.getLogger(__name__)


def configured() -> bool:
    s = get_settings()
    return bool(s.smtp_user and s.smtp_password)


def _send(to: str, subject: str, body: str, html: str | None = None) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_user, to, subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")  # clients without HTML show the text part
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


async def send_email(to: str, subject: str, body: str, html: str | None = None) -> None:
    """Send now, or raise AppError (503 not configured / 502 send failed)."""
    if not configured():
        raise AppError(
            503,
            "EMAIL_NOT_CONFIGURED",
            "Email isn't set up on the server. Share the link instead.",
        )
    try:
        await asyncio.to_thread(_send, to, subject, body, html)
    except (smtplib.SMTPException, OSError) as e:
        # the body holds a sign-in link: log the error type only, never the message
        log.warning("email to %s failed: %s", to, type(e).__name__)
        raise AppError(502, "EMAIL_FAILED", "Couldn't send the email. Try again.") from e

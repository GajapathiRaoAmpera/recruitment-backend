"""Tell candidates they were shortlisted / advanced and how to start the next round.

Sent after the HR action commits, in the background: a mail outage never undoes a shortlist or an
advance. Only a sign-in link goes out: students still sign in with Student ID + DOB (no tokens,
no DOB, no test content in the email)."""

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import email
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.mail_template import render

log = logging.getLogger(__name__)

LABELS = {
    "aptitude": "Aptitude test",
    "coding": "Coding test",
    "technical": "Technical test",
    "ai_interview": "AI interview",
    "final": "Final round",
}

Mail = tuple[str, str, str, str]  # to, subject, text, html


def compose(row, shortlisted: bool, link: str) -> Mail:
    rnd = LABELS.get(row["round_type"], row["round_type"].replace("_", " ").title())
    role, company = row["role_title"], row["company"]
    if shortlisted:
        subject = f"You're shortlisted: {role} at {company}"
        heading = "You've been shortlisted"
        lead = f"Good news! Your resume has been shortlisted for the {role} role at {company}."
    else:
        subject = f"You're through to the {rnd}: {role} at {company}"
        heading = "You're through to the next round"
        lead = f"Congratulations! You've cleared the previous round for {role} at {company}."
    how = (
        f"Your next step is the {rnd}. Sign in with your Student ID and date of birth to start it."
    )
    note = (
        "Use a laptop with a stable connection, and keep this tab the only one open "
        "while you take a test."
    )
    details = [("Company", company), ("Role", role), ("Next round", rnd)]
    name = row["full_name"]
    body = f"Hi {name},\n\n{lead}\n\n{how}\n\n{link}\n\n{note}\n"
    html = render(heading, f"Hi {name},", [lead, how], f"Start {rnd}", link, note, details)
    return row["email"], subject, body, html


async def for_round(
    session: AsyncSession, round_id: UUID | None, candidate_ids: list[UUID], shortlisted: bool
) -> list[Mail]:
    """Build the emails now (inside the request's session); send them later with send_all."""
    if round_id is None or not candidate_ids:
        return []
    result = await session.execute(
        text(
            "select c.full_name, c.email, r.type::text as round_type, d.company, d.role_title "
            "from rounds r join drives d on d.id = r.drive_id "
            "join candidates c on c.drive_id = d.id "
            "where r.id = :r and c.id = any(:ids) and c.email <> ''"
        ),
        {"r": round_id, "ids": candidate_ids},
    )
    link = f"{get_settings().frontend_url.rstrip('/')}/student/login"
    return [compose(row, shortlisted, link) for row in result.mappings()]


async def send_all(mails: list[Mail]) -> None:
    """Background task: the HR action already committed, so failures are only logged."""
    if not mails:
        return
    if not email.configured():
        log.warning("candidate emails skipped (SMTP not configured): %d", len(mails))
        return
    failed = 0
    # shortcut: one SMTP login per email; batch on one connection if drives pass ~500 students
    for mail in mails:
        try:
            await email.send_email(*mail)
        except AppError:
            failed += 1
    if failed:
        log.warning("candidate emails failed: %d of %d", failed, len(mails))

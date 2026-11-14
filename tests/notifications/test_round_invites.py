import asyncio

from app.core.errors import AppError
from app.notifications import round_invites as ri

ROW = {
    "full_name": "Asha <b>Rao</b>",
    "email": "asha@x.com",
    "round_type": "ai_interview",
    "company": "Ampera",
    "role_title": "SDE",
}
LINK = "http://app/student/login"


def test_shortlist_and_advance_emails():
    to, subject, body, html = ri.compose(ROW, True, LINK)
    assert to == "asha@x.com" and "shortlisted" in subject and LINK in body
    assert "AI interview" in body and f'href="{LINK}"' in html
    assert "<b>Rao</b>" not in html and "&lt;b&gt;" in html  # names are escaped
    _, subject, _, html = ri.compose(ROW, False, LINK)
    assert "AI interview" in subject and "next round" in html


def test_send_all_logs_failures_and_skips_when_unconfigured(monkeypatch):
    sent = []

    async def send(to, subject, body, html):
        if to == "bad@x.com":
            raise AppError(502, "EMAIL_FAILED", "down")
        sent.append(to)

    monkeypatch.setattr(ri.email, "send_email", send)
    mails = [("bad@x.com", "s", "b", "h"), ("ok@x.com", "s", "b", "h")]
    monkeypatch.setattr(ri.email, "configured", lambda: False)
    asyncio.run(ri.send_all(mails))
    assert sent == []
    monkeypatch.setattr(ri.email, "configured", lambda: True)
    asyncio.run(ri.send_all(mails))  # one failure doesn't stop the rest
    assert sent == ["ok@x.com"]

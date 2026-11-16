import asyncio

from app.core.errors import AppError
from app.core.mail_template import render
from app.notifications import round_invites as ri

ROW = {
    "full_name": "Asha <b>Rao</b>",
    "email": "asha@x.com",
    "round_type": "ai_interview",
    "company": "Ampera",
    "role_title": "SDE",
}
LINK = "http://app/student/login"


def test_shortlist_email_has_no_test_link():
    # shortlisting happens before HR sends a paper; the "is ready" email carries the link
    to, subject, body, html = ri.compose(ROW, True, LINK)
    assert to == "asha@x.com" and "selected" in subject
    assert LINK not in body and LINK not in html
    assert "Start" not in html and "Button not working" not in html
    assert "<b>Rao</b>" not in html and "&lt;b&gt;" in html  # names are escaped


def test_advance_email():
    _, subject, _, html = ri.compose(ROW, False, LINK)
    assert "AI interview" in subject and "next round" in html


def test_render_without_button_leaves_out_button_block():
    html = render("H", "Hi A,", ["Lead"], details=[("Company", "Ampera")])
    assert "href=" not in html and "Button not working" not in html
    assert "Lead" in html and "Ampera" in html


def test_render_with_button_includes_button_block():
    html = render("H", "Hi A,", ["Lead"], "Go now", LINK, "note")
    assert html.count(f'href="{LINK}"') == 2 and "Go now" in html
    assert "Button not working" in html and "note" in html


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


def test_paper_ready_email():
    _, subject, body, html = ri.compose({**ROW, "round_type": "aptitude"}, False, LINK, ready=True)
    assert subject == "Your Aptitude test is ready: SDE at Ampera"
    assert "question paper" in body and LINK in body and "Your Aptitude test is ready" in html

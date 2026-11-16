"""One branded HTML email layout (tables + inline CSS: what Gmail/Outlook actually render)."""

from html import escape

MAGENTA, INK, MUTED, BORDER = "#9c369a", "#050505", "#5b5b5b", "#e7e5e4"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def render(
    heading: str,
    greeting: str,
    paragraphs: list[str],
    button: str,
    link: str,
    note: str = "",
    details: list[tuple[str, str]] | None = None,
) -> str:
    """All text is escaped here; callers pass plain strings."""
    e = escape
    preview = e(paragraphs[0]) if paragraphs else ""  # inbox preview line
    paras = "".join(
        f'<p style="margin:0 0 16px;font-size:15px;line-height:24px;color:{INK}">{e(p)}</p>'
        for p in paragraphs
    )
    rows = "".join(
        f'<tr><td style="padding:6px 0;font-size:13px;color:{MUTED};width:120px">{e(k)}</td>'
        f'<td style="padding:6px 0;font-size:14px;color:{INK};font-weight:600">{e(v)}</td></tr>'
        for k, v in details or []
    )
    box = (
        f'<table role="presentation" width="100%" style="margin:0 0 24px;background:#f7edf7;'
        f'border-radius:8px"><tr><td style="padding:12px 16px"><table role="presentation" '
        f'width="100%">{rows}</table></td></tr></table>'
        if rows
        else ""
    )
    foot = (
        f'<p style="margin:24px 0 0;font-size:13px;line-height:20px;color:{MUTED}">{e(note)}</p>'
        if note
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta name="color-scheme" content="light"><title>{e(heading)}</title></head>
<body style="margin:0;padding:0;background:#f5f5f4;font-family:{FONT}">
<span style="display:none;max-height:0;overflow:hidden">{preview}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f4">
<tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="max-width:560px;background:#ffffff;border:1px solid {BORDER};border-radius:12px">
<tr><td style="padding:20px 32px;border-bottom:1px solid {BORDER}">
<span style="font-size:16px;font-weight:700;color:{MAGENTA}">Ampera</span>
<span style="font-size:16px;color:{INK}"> Campus Hire</span></td></tr>
<tr><td style="padding:32px">
<h1 style="margin:0 0 16px;font-size:22px;line-height:30px;color:{INK}">{e(heading)}</h1>
<p style="margin:0 0 16px;font-size:15px;line-height:24px;color:{INK}">{e(greeting)}</p>
{paras}{box}
<table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td style="border-radius:8px;background:{MAGENTA}">
<a href="{e(link)}" style="display:inline-block;padding:12px 24px;font-size:15px;font-weight:600;
color:#ffffff;text-decoration:none;border-radius:8px">{e(button)}</a></td></tr></table>
<p style="margin:24px 0 0;font-size:13px;line-height:20px;color:{MUTED}">
Button not working? Paste this link into your browser:<br>
<a href="{e(link)}" style="color:{MAGENTA};word-break:break-all">{e(link)}</a></p>
{foot}
</td></tr></table>
<p style="margin:16px 0 0;font-size:12px;color:{MUTED}">
This is an automated message from Ampera Campus Hire. Please don't reply.</p>
</td></tr></table></body></html>"""

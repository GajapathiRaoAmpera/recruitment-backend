"""Download a resume from a Google Drive link in the form export.

We only ever fetch from Google's download host, with a file id we extracted ourselves,
so a hostile cell value cannot make the server request arbitrary URLs.
"""

import re

import httpx

MAX_PDF = 5 * 1024 * 1024
_ID = re.compile(r"(?:[?&]id=|/d/|/file/d/)([A-Za-z0-9_-]{15,})")
_TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests inject a fake


class DriveError(Exception):
    def __init__(self, code: str):
        self.code = code  # not_accessible | not_pdf | too_large


def resume_link(form_data: dict) -> str | None:
    """The cell under the 'Upload your ... Resume' column."""
    for key, value in form_data.items():
        if "resume" in key.lower() and isinstance(value, str) and value.strip():
            return value
    return None


def file_id(link: str) -> str | None:
    """First Drive file id in the cell (a form can store several, comma-separated)."""
    m = _ID.search(link)
    return m.group(1) if m else None


async def fetch_pdf(fid: str) -> bytes:
    url = "https://drive.usercontent.google.com/download"
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, transport=_TRANSPORT
        ) as client:
            async with client.stream(
                "GET", url, params={"id": fid, "export": "download", "confirm": "t"}
            ) as r:
                if r.status_code != 200 or "text/html" in r.headers.get("content-type", ""):
                    raise DriveError("not_accessible")  # private file, deleted, or sign-in page
                data = b""
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_PDF:
                        raise DriveError("too_large")
    except httpx.HTTPError as e:
        raise DriveError("not_accessible") from e
    if not data.startswith(b"%PDF"):
        raise DriveError("not_pdf")  # e.g. a .docx
    return data

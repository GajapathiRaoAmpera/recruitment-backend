import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth.schemas import StaffUser
from app.auth.security import current_user
from app.candidates import drive, repo, storage
from app.core.db import get_session
from app.main import app
from tests.candidates.test_endpoints import PDF


@pytest.fixture
def anyio_backend():
    return "asyncio"


FID = "1AbCdEfGhIjKlMnOpQrStUvWxYz"
COL = "Upload your Updated Resume here"


@pytest.mark.parametrize(
    "link",
    [
        f"https://drive.google.com/open?id={FID}",
        f"https://drive.google.com/file/d/{FID}/view?usp=sharing",
        f"https://drive.google.com/uc?export=download&id={FID}",
        f"https://drive.google.com/open?id={FID}, https://drive.google.com/open?id=ZZZZZZZZZZZZZZZZZZZZ",
    ],
)
def test_file_id_from_common_link_shapes(link):
    assert drive.file_id(link) == FID


def test_file_id_rejects_other_urls():
    assert drive.file_id("https://evil.com/steal") is None
    assert drive.file_id("") is None


def test_resume_link_picks_resume_column():
    assert drive.resume_link({"Branch": "CSE", COL: "L"}) == "L"
    assert drive.resume_link({"Branch": "CSE"}) is None


def _serve(monkeypatch, handler):
    monkeypatch.setattr(drive, "_TRANSPORT", httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_fetch_ok_and_only_google_host(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    _serve(monkeypatch, handler)
    assert await drive.fetch_pdf(FID) == PDF
    assert seen == ["drive.usercontent.google.com"]


@pytest.mark.parametrize(
    "resp,code",
    [
        (
            httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html"}),
            "not_accessible",
        ),
        (httpx.Response(404), "not_accessible"),
        (
            httpx.Response(
                200, content=b"PK\x03\x04docx", headers={"content-type": "application/octet-stream"}
            ),
            "not_pdf",
        ),
        (httpx.Response(200, content=b"%PDF" + b"x" * drive.MAX_PDF), "too_large"),
    ],
)
@pytest.mark.anyio
async def test_fetch_failures(monkeypatch, resp, code):
    _serve(monkeypatch, lambda req: resp)
    with pytest.raises(drive.DriveError) as e:
        await drive.fetch_pdf(FID)
    assert e.value.code == code


# --- endpoint ---
DRIVE = uuid.uuid4()
HR = StaffUser(id=uuid.uuid4(), email="h@x.com", full_name="H", role="hr")
SAVED: list = []


class FakeSession:
    async def commit(self):
        pass


@pytest.fixture
def api(monkeypatch):
    SAVED.clear()
    people = [
        {
            "id": uuid.uuid4(),
            "student_id": "A",
            "form_data": {COL: f"https://drive.google.com/open?id={FID}"},
        },
        {"id": uuid.uuid4(), "student_id": "B", "form_data": {}},
        {
            "id": uuid.uuid4(),
            "student_id": "C",
            "form_data": {COL: "https://drive.google.com/open?id=PRIVATEPRIVATEPRIVATE"},
        },
    ]

    async def get_drive(_, d, o):
        return {"id": DRIVE, "jd_text": "Java"} if d == DRIVE else None

    async def todo(*_):
        return people

    async def fetch_pdf(fid):
        if fid == FID:
            return PDF
        raise drive.DriveError("not_accessible")

    async def upload(d, c, data):
        return f"{d}/{c}.pdf"

    async def upsert(_, cid, path, parsed):
        SAVED.append((cid, parsed, path))

    async def audit(*a, **k):
        return None

    monkeypatch.setattr(repo, "get_drive", get_drive)
    monkeypatch.setattr(repo, "candidates_to_fetch", todo)
    monkeypatch.setattr(repo, "upsert_resume", upsert)
    monkeypatch.setattr(drive, "fetch_pdf", fetch_pdf)
    monkeypatch.setattr(storage, "upload_pdf", upload)
    monkeypatch.setattr("app.candidates.router.audit", audit)
    app.dependency_overrides[get_session] = lambda: FakeSession()
    app.dependency_overrides[current_user] = lambda: HR
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_fetch_endpoint_reports_each_candidate(api):
    r = api.post(f"/api/v1/drives/{DRIVE}/resumes/fetch")
    assert [(x["student_id"], x["status"]) for x in r.json()["results"]] == [
        ("A", "parsed"),
        ("B", "no_link"),
        ("C", "not_accessible"),
    ]
    assert len(SAVED) == 1 and SAVED[0][1]["skills"] == ["java", "python"]
    assert SAVED[0][2].endswith(".pdf")  # saved file key


def test_fetch_other_drive_404(api):
    assert api.post(f"/api/v1/drives/{uuid.uuid4()}/resumes/fetch").status_code == 404

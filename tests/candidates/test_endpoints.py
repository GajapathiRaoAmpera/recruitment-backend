"""Resume upload, filters and shortlist: ownership checks and the parse path with a real PDF."""

import io
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.auth.schemas import StaffUser
from app.auth.security import current_user
from app.candidates import parser, repo, router, storage
from app.core.db import get_session
from app.main import app

DRIVE, CAND, ROUND_ONE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
HR = StaffUser(id=uuid.uuid4(), email="h@x.com", full_name="H", role="hr")
OTHER_HR_DRIVE_OWNER = uuid.uuid4()
SAVED: list = []
QUERY: dict = {}


def make_pdf(text: str) -> bytes:
    """Smallest valid one-page PDF with a text line."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1,
        xref,
    )


class FakeSession:
    async def commit(self):
        pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    SAVED.clear()
    QUERY.clear()
    monkeypatch.setattr(router, "audit", lambda *a, **k: _noop())

    async def get_drive(_, drive_id, owner):
        mine = drive_id == DRIVE and owner in (None, HR.id)
        return {"id": DRIVE, "jd_text": "Need Java and SQL"} if mine else None

    async def cands(_, d):
        return [
            {
                "id": CAND,
                "student_id": "S101",
                "full_name": "Asha Rao",
                "email": "asha@x.com",
                "phone": "+91 98765 43210",
            },
            {
                "id": uuid.uuid4(),
                "student_id": "S102",
                "full_name": "Ravi Kumar",
                "email": "ravi@x.com",
                "phone": None,
            },
        ]

    async def in_drive(_, d, c):
        return c == CAND

    async def upsert(_, cid, path, parsed):
        SAVED.append((cid, path, parsed))

    async def upload(d, c, data):
        return f"{d}/{c}.pdf"

    async def list_candidates(_, d, *args):
        QUERY["args"] = args
        return []

    async def set_sl(_, d, ids, v):
        QUERY["sl"] = (ids, v)
        return len(ids)

    async def sync(_, d, rid, ids, v):
        QUERY["sync"] = (rid, ids, v)
        return len(ids)

    async def ensure(*_):
        return None

    async def first_round(*_):
        return ROUND_ONE

    monkeypatch.setattr(repo, "get_drive", get_drive)
    monkeypatch.setattr(repo, "drive_candidates", cands)
    monkeypatch.setattr(repo, "candidate_in_drive", in_drive)
    monkeypatch.setattr(repo, "upsert_resume", upsert)
    monkeypatch.setattr(repo, "list_candidates", list_candidates)
    monkeypatch.setattr(repo, "set_shortlisted", set_sl)
    monkeypatch.setattr(repo, "sync_round_one", sync)
    monkeypatch.setattr(router.rounds_mod, "ensure_rounds", ensure)
    monkeypatch.setattr(router.rounds_mod, "first_test_round", first_round)
    monkeypatch.setattr(storage, "upload_pdf", upload)
    app.dependency_overrides[get_session] = lambda: FakeSession()
    app.dependency_overrides[current_user] = lambda: HR
    yield
    app.dependency_overrides.clear()


async def _noop():
    return None


client = TestClient(app)
PDF = make_pdf("Asha Rao asha@x.com Skills: Java, Python. CGPA: 8.4")


def upload(files):
    return client.post(f"/api/v1/drives/{DRIVE}/resumes", files=[("file", f[1]) for f in files])


def test_pdf_parsed_and_saved():
    r = upload([("files", ("final_resume_v3.pdf", PDF, "application/pdf"))])
    assert r.json()["results"][0]["status"] == "parsed"
    assert r.json()["results"][0]["candidate_id"] == str(CAND)
    _, path, parsed = SAVED[0]
    assert parsed["skills"] == ["java", "python"] and parsed["cgpa"] == 8.4
    assert parsed["jd_match"] == 50.0  # JD wants java + sql, resume has java


def test_zip_and_unmatched_and_non_pdf():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("folder/Resume (2).PDF", PDF)  # random name, folders ignored
        z.writestr("nobody.pdf", make_pdf("Somebody Else, nobody@x.com " + "x" * 30))
        z.writestr("notes.txt", "x")
    r = upload([("files", ("all.zip", buf.getvalue(), "application/zip"))])
    assert {x["file"]: x["status"] for x in r.json()["results"]} == {
        "Resume (2).PDF": "parsed",
        "nobody.pdf": "unmatched",
    }


def test_scanned_pdf_falls_back_to_ocr(monkeypatch):
    monkeypatch.setattr(
        parser, "_ocr_text", lambda d: "Asha Rao asha@x.com. Skills: SQL. CGPA 9.1/10"
    )
    r = upload([("files", ("scan.pdf", make_pdf("hi"), "application/pdf"))])
    assert r.json()["results"][0]["status"] == "parsed" and SAVED[0][2]["cgpa"] == 9.1


def test_corrupt_pdf_unmatched_unless_named_by_id():
    r = upload([("files", ("x.pdf", b"garbage", "application/pdf"))])
    assert r.json()["results"][0]["status"] == "unmatched" and SAVED == []
    r = upload([("files", ("S101.pdf", b"garbage", "application/pdf"))])  # filename still works
    assert r.json()["results"][0]["status"] == "parse_failed"


def test_attach_manually():
    url = f"/api/v1/drives/{DRIVE}/candidates/{CAND}/resume"
    r = client.put(url, files={"file": ("any.pdf", PDF, "application/pdf")})
    assert r.json()["status"] == "parsed" and SAVED[0][0] == CAND
    other = f"/api/v1/drives/{DRIVE}/candidates/{uuid.uuid4()}/resume"
    r = client.put(other, files={"file": ("any.pdf", PDF, "application/pdf")})
    assert r.status_code == 404


def test_other_users_drive_is_404():
    r = client.get(f"/api/v1/drives/{uuid.uuid4()}/candidates")
    assert r.status_code == 404 and r.json()["error"]["code"] == "DRIVE_NOT_FOUND"


def test_filters_passed_through():
    r = client.get(
        f"/api/v1/drives/{DRIVE}/candidates?skills=Java, SQL&min_cgpa=7"
        "&min_match=60&shortlisted=false"
    )
    assert r.status_code == 200 and r.json() == {"items": [], "next_cursor": None}
    assert QUERY["args"] == (["java", "sql"], 7.0, 60.0, False, 50, 0)


def test_bad_filter_values_422():
    assert client.get(f"/api/v1/drives/{DRIVE}/candidates?min_cgpa=11").status_code == 422
    assert client.get(f"/api/v1/drives/{DRIVE}/candidates?cursor=abc").status_code == 422


def test_shortlist():
    ids = [str(CAND)]
    r = client.post(
        f"/api/v1/drives/{DRIVE}/shortlist", json={"candidate_ids": ids, "shortlisted": True}
    )
    assert r.json() == {"updated": 1, "moved": 1} and QUERY["sl"][1] is True
    assert QUERY["sync"] == (ROUND_ONE, [CAND], True)  # invited to Round 1
    assert (
        client.post(
            f"/api/v1/drives/{DRIVE}/shortlist", json={"candidate_ids": [], "shortlisted": True}
        ).status_code
        == 422
    )


def test_student_token_cannot_reach_hr_routes():
    app.dependency_overrides.pop(current_user)
    r = client.get(f"/api/v1/drives/{DRIVE}/candidates", headers={"Authorization": "Bearer x"})
    assert r.status_code == 401


PROFILE = {
    "id": CAND,
    "drive_id": DRIVE,
    "student_id": "S101",
    "full_name": "Asha Rao",
    "email": "asha@x.com",
    "phone": None,
    "form_data": {"branch": "CSE"},
    "storage_path": "d/c.pdf",
    "skills": ["java"],
    "cgpa": 8.4,
    "jd_match": 50.0,
    "parse_status": "ok",
    "shortlisted": None,
    "rounds": [
        {
            "round_id": uuid.uuid4(),
            "position": 1,
            "type": "aptitude",
            "status": "invited",
            "score": None,
            "rank": None,
            "hr_note": None,
        }
    ],
}


def test_profile(monkeypatch):
    async def profile(_, cid, owner):
        return PROFILE if cid == CAND and owner == HR.id else None

    monkeypatch.setattr(repo, "get_profile", profile)
    body = client.get(f"/api/v1/candidates/{CAND}").json()
    assert (
        body["resume"]["url"] == f"/api/v1/candidates/{CAND}/resume"
        and body["resume"]["cgpa"] == 8.4
    )
    assert body["rounds"][0]["type"] == "aptitude" and body["form_data"] == {"branch": "CSE"}
    assert "dob" not in body and "extracted_text" not in body and "storage_path" not in body
    assert client.get(f"/api/v1/candidates/{uuid.uuid4()}").status_code == 404


def test_profile_without_resume(monkeypatch):
    async def profile(*_):
        return {**PROFILE, "storage_path": None}

    monkeypatch.setattr(repo, "get_profile", profile)
    assert client.get(f"/api/v1/candidates/{CAND}").json()["resume"] is None


def test_resume_download_and_missing(monkeypatch, tmp_path):
    f = tmp_path / "c.pdf"
    f.write_bytes(PDF)

    async def profile(_, cid, owner):
        return PROFILE if cid == CAND else None

    monkeypatch.setattr(repo, "get_profile", profile)
    monkeypatch.setattr(storage, "file_path", lambda key: f if key == "d/c.pdf" else None)
    r = client.get(f"/api/v1/candidates/{CAND}/resume")
    assert (
        r.status_code == 200 and r.content == PDF and r.headers["content-type"] == "application/pdf"
    )
    assert client.get(f"/api/v1/candidates/{uuid.uuid4()}/resume").status_code == 404
    monkeypatch.setattr(storage, "file_path", lambda key: None)  # row exists, file gone
    assert client.get(f"/api/v1/candidates/{CAND}/resume").status_code == 404


def test_local_storage_roundtrip_and_no_escape(monkeypatch, tmp_path):
    from app.core.config import get_settings
    from app.core.errors import AppError

    monkeypatch.setattr(get_settings(), "resume_dir", str(tmp_path))
    d, c = uuid.uuid4(), uuid.uuid4()
    key = f"{d}/{c}.pdf"
    storage._write(key, PDF)  # upload_pdf itself is faked by the autouse fixture
    assert storage.file_path(key).read_bytes() == PDF
    assert storage.file_path(f"{d}/missing.pdf") is None
    with pytest.raises(AppError):
        storage.file_path("../../etc/passwd")

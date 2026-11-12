"""Import with the real Google Form headers, as .xlsx and .csv."""

import io
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.auth.schemas import StaffUser
from app.auth.security import current_user
from app.candidates import importer, repo
from app.core.db import get_session
from app.main import app

HEADERS = [
    "Timestamp",
    "Email Address",
    "Full name",
    "College Student ID / Roll no.",
    "Date of birth",
    "Phone",
    "College name",
    "Degree",
    "Branch / Department",
    "Graduation year",
    "CGPA / Percentage",
    "Number of active backlogs",
    "10th percentage",
    "12th / Diploma percentage",
    "Key skills",
    "I agree to data processing and proctoring (location, full-screen, tab tracking).",
    "Upload your Updated Resume here",
]
DRIVE = uuid.uuid4()
HR = StaffUser(id=uuid.uuid4(), email="h@x.com", full_name="H", role="hr")
SAVED: dict = {}


def row(sid="21CS1001", name="Asha Rao", email="Asha@X.com", dob="2003-05-01", **over):
    base = [
        "2026-01-01 10:00",
        email,
        name,
        sid,
        dob,
        "9876543210",
        "ABC College",
        "B.Tech",
        "CSE",
        2026,
        "8.5",
        0,
        "90",
        "88",
        "Java, SQL",
        "Yes",
        "https://drive.google.com/open?id=1",
    ]
    return base if not over else base[:4] + [over.get("dob", dob)] + base[5:]


def xlsx(rows, headers=HEADERS):
    wb = Workbook()
    wb.active.append(headers)
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class FakeSession:
    async def commit(self):
        pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    SAVED.clear()

    async def get_drive(_, d, owner):
        return {"id": DRIVE, "jd_text": ""} if d == DRIVE else None

    async def upsert(_, d, c):
        new = c["student_id"] not in SAVED
        SAVED[c["student_id"]] = c
        return new

    async def audit(*a, **k):
        return None

    monkeypatch.setattr(repo, "get_drive", get_drive)
    monkeypatch.setattr(repo, "upsert_candidate", upsert)
    monkeypatch.setattr("app.candidates.router.audit", audit)
    app.dependency_overrides[get_session] = lambda: FakeSession()
    app.dependency_overrides[current_user] = lambda: HR
    yield
    app.dependency_overrides.clear()


client = TestClient(app)


def do_import(name, data, drive=DRIVE):
    return client.post(f"/api/v1/drives/{drive}/candidates/import", files={"sheet": (name, data)})


def test_xlsx_import_maps_columns_and_keeps_rest_in_form_data():
    data = xlsx([row(), row("21CS1002", "Ravi Kumar", "ravi@x.com", dob=datetime(2002, 12, 31))])
    r = do_import("form.xlsx", data)
    assert r.json() == {"created": 2, "updated": 0, "errors": []}
    a = SAVED["21CS1001"]
    assert (
        a["email"] == "asha@x.com" and a["phone"] == "9876543210" and str(a["dob"]) == "2003-05-01"
    )
    assert a["form_data"]["Branch / Department"] == "CSE"
    assert a["form_data"]["Upload your Updated Resume here"].startswith("https://drive")
    assert "Email Address" not in a["form_data"] and "Date of birth" not in a["form_data"]
    assert str(SAVED["21CS1002"]["dob"]) == "2002-12-31"


def test_csv_with_indian_dates_and_resubmission_updates():
    csv_text = ",".join(f'"{h}"' for h in HEADERS) + "\n"
    for dob in ("05/03/2003", "06/03/2003"):  # same student twice: later row wins
        csv_text += (
            f'"t","a@x.com","Asha","21CS1","{dob}","1","c","b","d",2026,8,0,9,9,"Java","Yes","l"\n'
        )
    r = do_import("form.csv", csv_text.encode())
    assert r.json()["created"] == 1 and r.json()["updated"] == 1
    assert str(SAVED["21CS1"]["dob"]) == "2003-03-06"  # DD/MM


def test_bad_rows_reported_with_spreadsheet_row_numbers():
    data = xlsx(
        [
            row(),
            row("", "No Id"),
            row("S2", "Bad Mail", "nope"),
            row("S3", "No Dob", dob=""),
            row("S4", "Bad Dob", dob="31/31/2000"),
        ]
    )
    r = do_import("f.xlsx", data).json()
    assert r["created"] == 1
    assert [(e["row"], e["message"]) for e in r["errors"]] == [
        (3, "Student ID is empty"),
        (4, "Email is not valid"),
        (5, "Date of birth is missing or not a date"),
        (6, "Date of birth is missing or not a date"),
    ]


def test_missing_required_column_422():
    r = do_import("f.xlsx", xlsx([], headers=["Email Address", "Full name"]))
    assert r.status_code == 422 and r.json()["error"]["code"] == "MISSING_COLUMNS"


@pytest.mark.parametrize("name,data", [("f.txt", b"x"), ("f.xlsx", b"not excel"), ("f.csv", b"")])
def test_bad_files_422(name, data):
    assert do_import(name, data).status_code == 422


def test_other_drive_404():
    assert do_import("f.csv", b"a", drive=uuid.uuid4()).status_code == 404


def test_blank_rows_skipped():
    assert importer.parse_sheet("f.csv", b"Email,Full name,Student ID,DOB\n,,,\n")[0] == []

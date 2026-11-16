"""A Super Admin creates a drive for an HR owner, or hands an existing drive to another HR.

HR stay limited to their own drives (AC-1); this only decides WHO the owner is.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.schemas import StaffUser
from app.auth.security import current_user
from app.core.db import get_session
from app.main import app

HR = StaffUser(id=uuid.uuid4(), email="h@x.com", full_name="Hema", role="hr")
ADMIN = StaffUser(id=uuid.uuid4(), email="a@x.com", full_name="Asha", role="super_admin")
OWNER = uuid.uuid4()
BASE = {"company": "C", "campus": "K", "role_title": "R", "jd_text": "J"}
DRIVE = {"id": uuid.uuid4(), **BASE, "status": "draft", "candidates": 0}


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def one(self):
        return self.rows[0]


class Session:
    """Answers by statement: the owner lookup, the drive insert/update, audit and rounds."""

    def __init__(self, owner=None, drive_exists=True):
        self.owner, self.drive_exists = owner, drive_exists
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, params or {}))
        if "from users where id" in sql:
            return Result([self.owner] if self.owner else [])
        if "insert into drives" in sql:
            return Result([{**DRIVE, "owner_id": params["o"]}])
        if "update drives" in sql:
            return Result([{"previous": HR.id}] if self.drive_exists else [])
        if sql.startswith("select d.id"):
            return Result([{**DRIVE, "owner_id": OWNER, "owner_name": "Ravi"}])
        return Result([])

    async def commit(self):
        pass

    def sql(self, fragment):
        return [p for s, p in self.calls if fragment in s]


def use(session, user):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


ACTIVE_HR = {"full_name": "Ravi", "role": "hr", "is_active": True}


def test_super_admin_creates_a_drive_for_an_hr():
    s = Session(owner=ACTIVE_HR)
    r = use(s, ADMIN).post("/api/v1/drives", json={**BASE, "owner_id": str(OWNER)})
    assert r.status_code == 201
    assert s.sql("insert into drives")[0]["o"] == OWNER
    assert r.json()["owner_id"] == str(OWNER) and r.json()["owner_name"] == "Ravi"
    audit = [p for sql, p in s.calls if "audit" in sql]
    assert audit and str(OWNER) in str(audit[0])


def test_hr_still_owns_what_they_create():
    s = Session()
    r = use(s, HR).post("/api/v1/drives", json=BASE)
    assert r.status_code == 201
    assert s.sql("insert into drives")[0]["o"] == HR.id
    assert not s.sql("from users where id")  # no lookup needed


def test_super_admin_without_owner_keeps_it():
    s = Session()
    assert use(s, ADMIN).post("/api/v1/drives", json=BASE).status_code == 201
    assert s.sql("insert into drives")[0]["o"] == ADMIN.id


def test_hr_cannot_create_a_drive_for_someone_else():
    s = Session(owner=ACTIVE_HR)
    r = use(s, HR).post("/api/v1/drives", json={**BASE, "owner_id": str(OWNER)})
    assert r.status_code == 403
    assert not s.sql("insert into drives")


@pytest.mark.parametrize(
    "owner",
    [
        None,  # unknown user
        {"full_name": "X", "role": "hr", "is_active": False},  # deactivated
        {"full_name": "X", "role": "super_admin", "is_active": True},  # not HR
    ],
)
def test_owner_must_be_an_active_hr(owner):
    s = Session(owner=owner)
    r = use(s, ADMIN).post("/api/v1/drives", json={**BASE, "owner_id": str(OWNER)})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_OWNER"
    assert not s.sql("insert into drives")


def test_super_admin_reassigns_a_drive():
    s = Session(owner=ACTIVE_HR)
    r = use(s, ADMIN).patch(f"/api/v1/drives/{DRIVE['id']}/owner", json={"owner_id": str(OWNER)})
    assert r.status_code == 200 and r.json()["owner_name"] == "Ravi"
    assert s.sql("update drives")[0] == {"o": OWNER, "id": DRIVE["id"]}
    audit = [p for sql, p in s.calls if "audit" in sql]
    assert audit and str(HR.id) in str(audit[0]) and str(OWNER) in str(audit[0])


def test_reassign_is_super_admin_only_and_404s_unknown_drives():
    assert (
        use(Session(owner=ACTIVE_HR), HR)
        .patch(f"/api/v1/drives/{DRIVE['id']}/owner", json={"owner_id": str(OWNER)})
        .status_code
        == 403
    )
    s = Session(owner=ACTIVE_HR, drive_exists=False)
    r = use(s, ADMIN).patch(f"/api/v1/drives/{uuid.uuid4()}/owner", json={"owner_id": str(OWNER)})
    assert r.status_code == 404 and r.json()["error"]["code"] == "DRIVE_NOT_FOUND"
    r = use(Session(owner=None), ADMIN).patch(
        f"/api/v1/drives/{DRIVE['id']}/owner", json={"owner_id": str(OWNER)}
    )
    assert r.status_code == 422

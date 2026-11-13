import uuid

from fastapi.testclient import TestClient

from app.auth.schemas import StaffUser
from app.auth.security import current_user
from app.core.db import get_session
from app.main import app

HR = StaffUser(id=uuid.uuid4(), email="h@x.com", full_name="H", role="hr")
DRIVE, ROUND, CID = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
ROW = {
    "id": ROUND, "type": "aptitude", "status": "active",  # the round lookup
    "candidate_id": CID, "student_id": "S1", "full_name": "Asha", "attempt_status": "submitted",
    "started_at": None, "submitted_at": None, "marks": 7, "tab_switches": 2,
    "step_id_dob": True, "step_location": True, "step_terms": False, "step_fullscreen": None,
}  # fmt: skip


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class Session:
    def __init__(self, rows):
        self.rows, self.params = rows, []

    async def execute(self, stmt, params=None):
        self.params.append(params)
        return Result(self.rows)


def client(rows, user=HR):
    s = Session(rows)
    app.dependency_overrides[get_session] = lambda: s
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app), s


def teardown_function():
    app.dependency_overrides.clear()


URL = f"/api/v1/drives/{DRIVE}/rounds/{ROUND}/progress"


def test_progress_lists_candidates_with_steps_and_attempt():
    c, s = client([ROW])
    r = c.get(URL)
    assert r.status_code == 200
    out = r.json()["candidates"][0]
    assert out["steps"] == {"id_dob": True, "location": True, "terms": False, "fullscreen": None}
    assert out["attempt_status"] == "submitted" and out["tab_switches"] == 2
    assert s.params[0]["o"] == HR.id  # HR is limited to their own drives


def test_progress_404_when_round_not_in_drive_or_not_owned():
    c, _ = client([])
    assert c.get(URL).json()["error"]["code"] == "ROUND_NOT_FOUND"


def test_super_admin_is_not_limited_to_owned_drives():
    admin = StaffUser(id=uuid.uuid4(), email="a@x.com", full_name="A", role="super_admin")
    c, s = client([ROW], admin)
    assert c.get(URL).status_code == 200 and s.params[0]["o"] is None


def test_progress_needs_login():
    app.dependency_overrides.clear()
    assert TestClient(app).get(URL).status_code == 401


def test_list_rounds_is_scoped_to_owner():
    c, s = client([{"id": ROUND, "position": 2, "type": "aptitude", "status": "active"}])
    r = c.get(f"/api/v1/drives/{DRIVE}/rounds")
    assert r.status_code == 200 and r.json()[0]["type"] == "aptitude"
    assert s.params[0]["o"] == HR.id

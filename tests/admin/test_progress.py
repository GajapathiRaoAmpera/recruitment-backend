import uuid

from fastapi.testclient import TestClient

from app.auth.schemas import StaffUser
from app.auth.security import current_user
from app.core.db import get_session
from app.main import app

D, R1, R2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
BASE = {"drive_id": D, "company": "C", "campus": "K", "role_title": "SDE", "status": "active"}
ROWS = [
    {**BASE, "round_id": R1, "position": 2, "type": "aptitude", "total": 5, "in_progress": 1,
     "submitted": 2, "locked": 1, "expired": 0},
    {**BASE, "round_id": R2, "position": 3, "type": "coding", "total": 0, "in_progress": 0,
     "submitted": 0, "locked": 0, "expired": 0},
]  # fmt: skip


class Session:
    async def execute(self, *_):
        class R:
            def mappings(self):
                return ROWS

        return R()


def user(role):
    return StaffUser(id=uuid.uuid4(), email="x@x.com", full_name="X", role=role)


def client(role):
    app.dependency_overrides[get_session] = lambda: Session()
    app.dependency_overrides[current_user] = lambda: user(role)
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_overview_groups_rounds_per_drive_with_not_started():
    r = client("super_admin").get("/api/v1/admin/progress")
    assert r.status_code == 200 and len(r.json()) == 1
    first, second = r.json()[0]["rounds"]
    assert first["not_started"] == 1 and first["submitted"] == 2 and second["total"] == 0


def test_hr_cannot_see_the_overview():
    assert client("hr").get("/api/v1/admin/progress").status_code == 403


def test_overview_needs_login():
    app.dependency_overrides.clear()
    assert TestClient(app).get("/api/v1/admin/progress").status_code == 401

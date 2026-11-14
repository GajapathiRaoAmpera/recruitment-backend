import uuid

from fastapi.testclient import TestClient

from app.auth.schemas import StaffUser
from app.auth.security import current_user
from app.core.db import get_session
from app.main import app

HR = StaffUser(id=uuid.uuid4(), email="h@x.com", full_name="H", role="hr")


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def one(self):
        return self.rows[0]


class Session:
    def __init__(self, rows):
        self.rows, self.params = rows, None

    async def execute(self, stmt, params=None):
        self.params = params
        return Result(self.rows)


ROW = {
    "id": uuid.uuid4(),
    "company": "C",
    "campus": "K",
    "role_title": "R",
    "jd_text": "J",
    "status": "draft",
    "candidates": 3,
}


def client(rows, user=HR):
    s = Session(rows)
    app.dependency_overrides[get_session] = lambda: s
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app), s


def teardown_function():
    app.dependency_overrides.clear()


def test_list_scopes_to_owner_and_admin_sees_all():
    c, s = client([ROW])
    assert c.get("/api/v1/drives").json()[0]["candidates"] == 3 and s.params["o"] == HR.id
    admin = StaffUser(id=uuid.uuid4(), email="a@x.com", full_name="A", role="super_admin")
    c, s = client([ROW], admin)
    c.get("/api/v1/drives")
    assert s.params["o"] is None


def test_get_one_and_404():
    c, _ = client([ROW])
    assert c.get(f"/api/v1/drives/{ROW['id']}").json()["role_title"] == "R"
    c, _ = client([])
    r = c.get(f"/api/v1/drives/{uuid.uuid4()}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "DRIVE_NOT_FOUND"


def test_create_saves_chosen_rounds_in_order():
    inserts = []

    class S(Session):
        async def execute(self, stmt, params=None):
            if "insert into rounds" in str(stmt):
                inserts.append(
                    (
                        params["pos"],
                        params["type"],
                        params["mode"],
                        params["status"],
                        params["threshold"],
                        params["duration"],
                    )
                )
                return Result([])
            return Result([{**ROW, "candidates": 0}])

        async def commit(self):
            pass

    app.dependency_overrides[get_session] = lambda: S([])
    app.dependency_overrides[current_user] = lambda: HR
    body = {
        "company": "C",
        "campus": "K",
        "role_title": "R",
        "jd_text": "J",
        "rounds": [
            {"type": "shortlist"},
            {"type": "aptitude", "threshold": 70, "duration_min": 45},
            {"type": "technical", "mode": "manual"},
            {"type": "final", "mode": "manual"},
        ],
    }
    r = TestClient(app).post("/api/v1/drives", json=body)
    assert r.status_code == 201
    assert inserts == [
        (0, "shortlist", "auto", "active", None, None),
        (1, "aptitude", "auto", "active", 70.0, 45.0),  # cut-off % and minutes are separate columns
        (2, "technical", "manual", "pending", None, None),
        (3, "final", "manual", "pending", None, None),
    ]


def test_create_without_rounds_uses_standard_pipeline():
    types = []

    class S(Session):
        async def execute(self, stmt, params=None):
            if "insert into rounds" in str(stmt):
                types.append(params["type"])
                return Result([])
            return Result([{**ROW, "candidates": 0}])

        async def commit(self):
            pass

    app.dependency_overrides[get_session] = lambda: S([])
    app.dependency_overrides[current_user] = lambda: HR
    base = {"company": "C", "campus": "K", "role_title": "R", "jd_text": "J"}
    assert TestClient(app).post("/api/v1/drives", json=base).status_code == 201
    assert types == ["shortlist", "aptitude", "coding", "ai_interview", "final"]


import pytest  # noqa: E402


@pytest.mark.parametrize(
    "rounds",
    [
        [{"type": "aptitude"}, {"type": "final"}],  # no shortlist first
        [{"type": "shortlist"}],  # too few
        [{"type": "shortlist"}, {"type": "shortlist"}],  # two shortlists
        [{"type": "shortlist"}, {"type": "technical"}, {"type": "technical"}],  # two technical
        [{"type": "shortlist"}, {"type": "nonsense"}],
    ],
)
def test_create_rejects_bad_rounds(rounds):
    c, _ = client([])
    base = {"company": "C", "campus": "K", "role_title": "R", "jd_text": "J"}
    assert c.post("/api/v1/drives", json={**base, "rounds": rounds}).status_code == 422

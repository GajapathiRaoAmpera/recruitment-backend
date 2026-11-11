"""FR-27 / AC-1: Super Admin manages staff users; HR cannot."""

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.admin import repo, service, supabase_admin
from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.core.errors import AppError
from app.main import app

ADMIN = StaffUser(id=uuid.uuid4(), email="sa@x.com", full_name="S A", role="super_admin")
HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
NOW = datetime.now(UTC)


class FakeSession:
    commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def db(monkeypatch):
    users = {u.id: {**u.model_dump(), "is_active": True, "created_at": NOW} for u in (ADMIN, HR)}
    audits, invites = [], []
    session = FakeSession()

    async def list_users(_):
        return list(users.values())

    async def get_user(_, uid):
        return users.get(uid)

    async def email_taken(_, email):
        return any(u["email"].lower() == email.lower() for u in users.values())

    async def insert_user(_, uid, email, full_name, role):
        users[uid] = dict(
            id=uid, email=email, full_name=full_name, role=role, is_active=True, created_at=NOW
        )
        return users[uid]

    async def update_user(_, uid, changes):
        users[uid] = {**users[uid], **changes}
        return users[uid]

    async def invite(email, full_name):
        invites.append((email, full_name))
        return uuid.uuid4()

    async def audit(_, actor_id, action, entity, entity_id, diff=None):
        audits.append((actor_id, action, entity_id, diff))

    for name, fn in dict(
        list_users=list_users,
        get_user=get_user,
        email_taken=email_taken,
        insert_user=insert_user,
        update_user=update_user,
    ).items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(supabase_admin, "invite_user", invite)
    monkeypatch.setattr(service, "audit", audit)
    app.dependency_overrides[get_session] = lambda: session
    yield dict(users=users, audits=audits, invites=invites, session=session)
    app.dependency_overrides.clear()


def as_user(user):
    app.dependency_overrides[security.current_user] = lambda: user
    return TestClient(app)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/admin/users"),
        ("post", "/api/v1/admin/users"),
        ("patch", f"/api/v1/admin/users/{uuid.uuid4()}"),
    ],
)
def test_hr_forbidden(db, method, path):
    r = as_user(HR).request(method.upper(), path, json={"email": "n@x.com", "full_name": "N"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_list(db):
    r = as_user(ADMIN).get("/api/v1/admin/users")
    assert r.status_code == 200
    assert {u["email"] for u in r.json()["items"]} == {"sa@x.com", "hr@x.com"}


def test_create_hr_invites_inserts_audits(db):
    r = as_user(ADMIN).post(
        "/api/v1/admin/users", json={"email": "new@x.com", "full_name": "  New Person "}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "hr" and body["full_name"] == "New Person" and body["is_active"]
    assert db["invites"] == [("new@x.com", "New Person")]
    assert db["audits"][0][:2] == (ADMIN.id, "user.create")
    assert db["session"].commits == 1


def test_create_duplicate_skips_invite(db):
    r = as_user(ADMIN).post("/api/v1/admin/users", json={"email": "HR@x.com", "full_name": "X"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "USER_EXISTS"
    assert db["invites"] == [] and db["session"].commits == 0


@pytest.mark.parametrize(
    "body",
    [
        {"email": "nope", "full_name": "X"},
        {"email": "a@x.com", "full_name": "  "},
        {"email": "a@x.com", "full_name": "X", "role": "student"},
    ],
)
def test_create_validation(db, body):
    r = as_user(ADMIN).post("/api/v1/admin/users", json=body)
    assert r.status_code == 422 and db["invites"] == []


def test_deactivate_other_user_audits_before_and_after(db):
    r = as_user(ADMIN).patch(f"/api/v1/admin/users/{HR.id}", json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    _, action, entity_id, diff = db["audits"][0]
    assert action == "user.update" and entity_id == HR.id
    assert diff == {"from": {"is_active": True}, "to": {"is_active": False}}


@pytest.mark.parametrize("change", [{"is_active": False}, {"role": "hr"}])
def test_cannot_lock_yourself_out(db, change):
    r = as_user(ADMIN).patch(f"/api/v1/admin/users/{ADMIN.id}", json=change)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CANNOT_CHANGE_SELF"
    assert db["audits"] == []


def test_rename_yourself_ok(db):
    r = as_user(ADMIN).patch(f"/api/v1/admin/users/{ADMIN.id}", json={"full_name": "Sam"})
    assert r.status_code == 200 and r.json()["full_name"] == "Sam"


def test_patch_unknown_404(db):
    r = as_user(ADMIN).patch(f"/api/v1/admin/users/{uuid.uuid4()}", json={"is_active": False})
    assert r.status_code == 404


# --- the Supabase invite call itself ---


@pytest.fixture
def supabase(monkeypatch):
    seen = {}
    real = httpx.AsyncClient

    def use(status, body):
        def handler(request):
            seen["request"] = request
            return httpx.Response(status, json=body)

        monkeypatch.setattr(
            supabase_admin.httpx,
            "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
        )
        return seen

    return use


def test_invite_sends_secret_server_side_and_redirect(supabase):
    uid = uuid.uuid4()
    seen = supabase(200, {"id": str(uid)})
    assert asyncio.run(supabase_admin.invite_user("a@x.com", "A")) == uid
    req = seen["request"]
    assert req.url.path == "/auth/v1/invite"
    assert req.headers["apikey"] == "sb_secret_test"
    assert req.url.params["redirect_to"] == "http://localhost:3000/set-password"


@pytest.mark.parametrize(
    "status,body,code",
    [
        (422, {"error_code": "email_exists"}, "USER_EXISTS"),
        (429, {}, "EMAIL_RATE_LIMIT"),
        (500, {}, "AUTH_PROVIDER_ERROR"),
    ],
)
def test_invite_errors(supabase, status, body, code):
    supabase(status, body)
    with pytest.raises(AppError) as e:
        asyncio.run(supabase_admin.invite_user("a@x.com", "A"))
    assert e.value.code == code

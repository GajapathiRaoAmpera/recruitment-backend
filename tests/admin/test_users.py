"""FR-27 / AC-1: Super Admin manages staff users; HR cannot."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.admin import repo, service
from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.core.errors import AppError
from app.main import app

ADMIN = StaffUser(id=uuid.uuid4(), email="sa@x.com", full_name="S A", role="super_admin")
HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
NOW = datetime.now(UTC)


class FakeSession:
    commits = rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def db(monkeypatch):
    users = {u.id: {**u.model_dump(), "is_active": True, "created_at": NOW} for u in (ADMIN, HR)}
    audits, invites, in_use = [], [], set()
    session = FakeSession()

    async def list_users(_):
        return list(users.values())

    async def get_user(_, uid):
        return users.get(uid)

    async def email_taken(_, email):
        return any(u["email"].lower() == email.lower() for u in users.values())

    async def insert_user(_, email, full_name, role):
        uid = uuid.uuid4()
        users[uid] = dict(
            id=uid, email=email, full_name=full_name, role=role, is_active=True, created_at=NOW
        )
        return users[uid]

    async def update_user(_, uid, changes):
        users[uid] = {**users[uid], **changes}
        return users[uid]

    async def delete_user(_, uid):
        if uid in in_use:
            raise IntegrityError("delete", {}, Exception("fk violation"))
        users.pop(uid)

    async def send_email(to, subject, body, html=None):
        invites.append((to, body))

    async def audit(_, actor_id, action, entity, entity_id, diff=None):
        audits.append((actor_id, action, entity_id, diff))

    for name, fn in dict(
        list_users=list_users,
        get_user=get_user,
        email_taken=email_taken,
        insert_user=insert_user,
        update_user=update_user,
        delete_user=delete_user,
    ).items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(service.email, "send_email", send_email)
    monkeypatch.setattr(service, "audit", audit)
    app.dependency_overrides[get_session] = lambda: session
    yield dict(
        users=users,
        audits=audits,
        invites=invites,
        session=session,
        in_use=in_use,
    )
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
        ("delete", f"/api/v1/admin/users/{uuid.uuid4()}"),
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
    [(to, mail)] = db["invites"]
    assert to == "new@x.com" and "Hi New Person" in mail
    assert "http://localhost:3000/set-password?token=" in mail
    assert db["audits"][0][:2] == (ADMIN.id, "user.create")
    assert db["session"].commits == 1


def test_create_with_link_returns_it_once_and_never_audits_it(db):
    r = as_user(ADMIN).post(
        "/api/v1/admin/users",
        json={"email": "link@x.com", "full_name": "Link", "send_email": False},
    )
    assert r.status_code == 201
    link = r.json()["invite_link"]
    assert link.startswith("http://localhost:3000/set-password?token=")
    assert db["invites"] == []  # nothing emailed
    assert "token" not in str(db["audits"])


def test_create_with_email_has_no_link(db):
    r = as_user(ADMIN).post("/api/v1/admin/users", json={"email": "e@x.com", "full_name": "E"})
    assert r.status_code == 201 and r.json()["invite_link"] is None


def test_super_admin_cannot_be_deleted(db):
    other = uuid.uuid4()
    db["users"][other] = {**db["users"][ADMIN.id], "id": other, "email": "sa2@x.com"}
    r = as_user(ADMIN).delete(f"/api/v1/admin/users/{other}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "SUPER_ADMIN_PROTECTED"
    assert other in db["users"]


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


def test_delete_unused_user_removes_row(db):
    r = as_user(ADMIN).delete(f"/api/v1/admin/users/{HR.id}")
    assert r.status_code == 204
    assert HR.id not in db["users"]
    assert db["audits"][0][1:3] == ("user.delete", HR.id) and db["session"].commits == 1


def test_delete_user_in_use_409_and_untouched(db):
    db["in_use"].add(HR.id)
    r = as_user(ADMIN).delete(f"/api/v1/admin/users/{HR.id}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "USER_IN_USE"
    assert "Deactivate" in r.json()["error"]["message"]
    assert HR.id in db["users"] and db["session"].commits == 0


def test_cannot_delete_yourself(db):
    r = as_user(ADMIN).delete(f"/api/v1/admin/users/{ADMIN.id}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "CANNOT_CHANGE_SELF"


def test_delete_unknown_404(db):
    assert as_user(ADMIN).delete(f"/api/v1/admin/users/{uuid.uuid4()}").status_code == 404


def test_invite_email_failure_creates_nothing(db, monkeypatch):
    async def boom(*_):
        raise AppError(502, "EMAIL_FAILED", "down")

    monkeypatch.setattr(service.email, "send_email", boom)
    r = as_user(ADMIN).post("/api/v1/admin/users", json={"email": "n@x.com", "full_name": "N"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "EMAIL_FAILED"
    assert db["session"].commits == 0  # the inserted row is rolled back with the session

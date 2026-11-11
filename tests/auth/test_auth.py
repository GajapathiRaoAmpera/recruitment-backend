"""AC-1: token verification and role checks for HR/Admin routes."""

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends
from fastapi.testclient import TestClient

from app.auth import security
from app.core.db import get_session
from app.main import app

KEY = ec.generate_private_key(ec.SECP256R1())
ISS = "https://test-project.supabase.co/auth/v1"
HR, ADMIN, INACTIVE, UNKNOWN = (uuid.uuid4() for _ in range(4))
USERS = {
    HR: {"id": HR, "email": "hr@x.com", "full_name": "H R", "role": "hr", "is_active": True},
    ADMIN: {
        "id": ADMIN,
        "email": "sa@x.com",
        "full_name": "S A",
        "role": "super_admin",
        "is_active": True,
    },
    INACTIVE: {
        "id": INACTIVE,
        "email": "old@x.com",
        "full_name": "Old",
        "role": "hr",
        "is_active": False,
    },
}


def token(sub=HR, key=KEY, alg="ES256", **over):
    claims = {"sub": str(sub), "aud": "authenticated", "iss": ISS, "exp": time.time() + 600}
    return jwt.encode({**claims, **over}, key, algorithm=alg)


def auth(t):
    return {"Authorization": f"Bearer {t}"}


@app.get("/_test/admin-only")
async def _admin_only(user=Depends(security.require_role("super_admin"))):
    return {"ok": True}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    async def signing_key(_):
        return KEY.public_key()

    async def get_user(_, user_id):
        return USERS.get(user_id)

    monkeypatch.setattr(security, "_signing_key", signing_key)
    monkeypatch.setattr(security.repo, "get_user", get_user)
    app.dependency_overrides[get_session] = lambda: None
    yield
    app.dependency_overrides.clear()


client = TestClient(app)


@pytest.mark.parametrize("sub,role", [(HR, "hr"), (ADMIN, "super_admin")])
def test_me_returns_role_from_db(sub, role):
    r = client.get("/api/v1/me", headers=auth(token(sub, role="super_admin")))
    assert r.status_code == 200
    assert r.json()["role"] == role  # a forged role claim is ignored
    assert "is_active" not in r.json()


def test_no_token_401():
    r = client.get("/api/v1/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize(
    "bad",
    [
        token(key=ec.generate_private_key(ec.SECP256R1())),  # wrong signer
        token(exp=time.time() - 10),  # expired
        token(aud="anon"),  # wrong audience
        token(iss="https://evil.supabase.co/auth/v1"),  # wrong project
        token(key="s" * 32, alg="HS256"),  # algorithm confusion
        token(sub="not-a-uuid"),
        "garbage",
    ],
)
def test_invalid_tokens_401(bad):
    r = client.get("/api/v1/me", headers=auth(bad))
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.parametrize("sub", [UNKNOWN, INACTIVE])
def test_not_staff_403(sub):
    r = client.get("/api/v1/me", headers=auth(token(sub)))
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_STAFF"


def test_hr_blocked_from_admin_route():
    r = client.get("/_test/admin-only", headers=auth(token(HR)))
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_admin_passes_admin_route():
    assert client.get("/_test/admin-only", headers=auth(token(ADMIN))).status_code == 200

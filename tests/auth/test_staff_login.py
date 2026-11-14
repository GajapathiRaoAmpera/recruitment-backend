"""Own staff auth: login + lockout, forgot-password, one-time set-password links (AC-1)."""

import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth import passwords, staff
from app.core.config import get_settings
from app.core.db import get_session
from app.main import app

PW = "correct horse battery"
HR = uuid.uuid4()


class FakeSession:
    commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def db(monkeypatch):
    user = dict(
        id=HR,
        email="hr@x.com",
        full_name="H R",
        role="hr",
        is_active=True,
        password_hash=passwords.hash_password(PW),
        password_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        failed_logins=0,
        locked_until=None,
    )
    users = {HR: user}
    sent, audits = [], []

    def view(u):
        locked = u["locked_until"] is not None and u["locked_until"] > datetime.now(UTC)
        return {**u, "locked": locked}

    async def get_login(_, email):
        return next((view(u) for u in users.values() if u["email"] == email.strip().lower()), None)

    async def get_password_state(_, uid):
        return users.get(uid)

    async def login_failed(_, uid, max_failures):
        u = users[uid]
        u["failed_logins"] += 1
        if u["failed_logins"] >= max_failures:
            u["failed_logins"], u["locked_until"] = 0, datetime.now(UTC) + timedelta(minutes=15)

    async def login_ok(_, uid):
        users[uid].update(failed_logins=0, locked_until=None)

    async def set_password(_, uid, h):
        users[uid].update(password_hash=h, password_set_at=datetime.now(UTC), locked_until=None)

    async def send_email(to, subject, body, html=None):
        sent.append((to, body))

    async def audit(_, actor, action, entity, entity_id, diff=None):
        audits.append((actor, action, diff))

    for name, fn in dict(
        get_login=get_login,
        get_password_state=get_password_state,
        login_failed=login_failed,
        login_ok=login_ok,
        set_password=set_password,
    ).items():
        monkeypatch.setattr(staff.repo, name, fn)
    monkeypatch.setattr(staff.email, "send_email", send_email)
    monkeypatch.setattr(staff, "audit", audit)
    session = FakeSession()
    app.dependency_overrides[get_session] = lambda: session
    yield dict(user=user, sent=sent, audits=audits, session=session)
    app.dependency_overrides.clear()


client = TestClient(app)


def login(email="hr@x.com", password=PW):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def link_token(body: str) -> str:
    url = next(w for w in body.split() if w.startswith("http"))
    return parse_qs(urlparse(url).query)["token"][0]


# --- passwords ---


def test_hash_roundtrip_and_salted():
    h = passwords.hash_password(PW)
    assert passwords.verify_password(PW, h) and not passwords.verify_password("nope", h)
    assert h != passwords.hash_password(PW) and PW not in h


@pytest.mark.parametrize("stored", [None, "", "bcrypt$x", "scrypt$1$2", "scrypt$a$b$c$d$e"])
def test_verify_never_raises(stored):
    assert passwords.verify_password(PW, stored) is False


# --- login ---


def test_login_returns_working_staff_token(db):
    r = login(email="  HR@x.com ")
    assert r.status_code == 200
    body = r.json()
    assert body["user"] == {"id": str(HR), "email": "hr@x.com", "full_name": "H R", "role": "hr"}
    assert body["expires_in"] == 8 * 3600
    claims = jwt.decode(body["access_token"], get_settings().jwt_secret, algorithms=["HS256"])
    assert claims["typ"] == "staff" and claims["sub"] == str(HR)


@pytest.mark.parametrize(
    "email,password,setup",
    [
        ("hr@x.com", "wrong password", None),
        ("nobody@x.com", PW, None),
        ("hr@x.com", PW, {"is_active": False}),
        ("hr@x.com", PW, {"password_hash": None}),  # invited, password never set
    ],
)
def test_bad_login_same_401(db, email, password, setup):
    db["user"].update(setup or {})
    r = login(email, password)
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert "access_token" not in r.text


def test_five_wrong_passwords_lock_even_the_right_one(db):
    for _ in range(5):
        assert login(password="wrong password").status_code == 401
    r = login()
    assert r.status_code == 429 and r.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"


def test_success_resets_the_failure_count(db):
    for _ in range(4):
        login(password="wrong password")
    assert login().status_code == 200
    assert db["user"]["failed_logins"] == 0


# --- forgot + set password ---


def test_forgot_password_emails_link_for_known_active_user(db):
    r = client.post("/api/v1/auth/forgot-password", json={"email": "hr@x.com"})
    assert r.status_code == 204
    [(to, body)] = db["sent"]
    assert to == "hr@x.com" and "http://localhost:3000/set-password?token=" in body


@pytest.mark.parametrize("email,setup", [("nobody@x.com", None), ("hr@x.com", False)])
def test_forgot_password_same_204_and_no_email(db, email, setup):
    if setup is False:
        db["user"]["is_active"] = False
    r = client.post("/api/v1/auth/forgot-password", json={"email": email})
    assert r.status_code == 204 and db["sent"] == []


def test_set_password_then_login_and_link_is_single_use(db):
    client.post("/api/v1/auth/forgot-password", json={"email": "hr@x.com"})
    tok = link_token(db["sent"][0][1])
    new = "a brand new password"
    r = client.post("/api/v1/auth/set-password", json={"token": tok, "password": new})
    assert r.status_code == 204
    assert login(password=new).status_code == 200 and login().status_code == 401
    assert db["audits"] == [(HR, "user.set_password", None)]
    again = client.post("/api/v1/auth/set-password", json={"token": tok, "password": "x" * 12})
    assert again.status_code == 400 and again.json()["error"]["code"] == "LINK_INVALID"


def test_invite_link_sets_first_password(db):
    db["user"].update(password_hash=None, password_set_at=None)
    tok = link_token(staff.set_password_link(HR, None, staff.INVITE_TTL))
    r = client.post("/api/v1/auth/set-password", json={"token": tok, "password": "first password"})
    assert r.status_code == 204 and login(password="first password").status_code == 200


def test_set_password_too_short_422_and_link_still_valid(db):
    tok = link_token(staff.set_password_link(HR, db["user"]["password_set_at"], 60))
    r = client.post("/api/v1/auth/set-password", json={"token": tok, "password": "short"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "WEAK_PASSWORD"
    ok = client.post("/api/v1/auth/set-password", json={"token": tok, "password": "long enough!"})
    assert ok.status_code == 204


def secret_token(**claims):
    base = {"typ": "set_password", "sub": str(HR), "exp": time.time() + 60, "pv": ""}
    return jwt.encode({**base, **claims}, get_settings().jwt_secret, algorithm="HS256")


@pytest.mark.parametrize(
    "tok",
    [
        "garbage",
        secret_token(exp=time.time() - 1),  # expired
        secret_token(typ="staff"),  # a session token is not a reset link
        secret_token(pv="2020-01-01T00:00:00+00:00"),  # older password version
        secret_token(sub=str(uuid.uuid4())),  # no such user
    ],
)
def test_bad_links_400(db, tok):
    r = client.post("/api/v1/auth/set-password", json={"token": tok, "password": "x" * 12})
    assert r.status_code == 400 and r.json()["error"]["code"] == "LINK_INVALID"
    assert db["session"].commits == 0


def test_inactive_user_cannot_use_link(db):
    tok = link_token(staff.set_password_link(HR, db["user"]["password_set_at"], 60))
    db["user"]["is_active"] = False
    r = client.post("/api/v1/auth/set-password", json={"token": tok, "password": "x" * 12})
    assert r.status_code == 400

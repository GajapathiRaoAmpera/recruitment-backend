"""AC-3: wrong ID/DOB rejected and logged; entry token gates the steps endpoint."""

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.auth import entry
from app.auth import repo as router_repo
from app.core.db import get_session
from app.main import app

CID, RID = uuid.uuid4(), uuid.uuid4()
ROW = {
    "candidate_id": CID,
    "student_id": "S101",
    "dob": date(2003, 5, 1),
    "full_name": "Asha",
    "round_id": RID,
    "round_type": "aptitude",
    "test_id": None,
    "duration_min": 30.0,
}
LOGS: list = []
FAILS = [0]
ROWS = [ROW]


class FakeSession:
    async def commit(self):
        pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    LOGS.clear()
    FAILS[0] = 0
    ROWS[:] = [ROW]

    async def find(_, sid):
        return ROWS if sid.strip().lower() == "s101" else []

    async def target(_, cid, rid):
        return ROW if (cid, rid) == (CID, RID) else None

    async def log(_, cid, rid, step, ok, meta):
        LOGS.append((cid, rid, step, ok))

    async def failures(*_):
        return FAILS[0]

    monkeypatch.setattr(entry.repo, "find_by_student_id", find)
    monkeypatch.setattr(entry.repo, "get_invite_target", target)
    monkeypatch.setattr(entry.repo, "log_step", log)
    monkeypatch.setattr(entry.repo, "recent_failures", failures)

    async def latest(_, cid, rid):
        return {"terms": True, "location": False}

    monkeypatch.setattr(router_repo, "latest_steps", latest)
    app.dependency_overrides[get_session] = lambda: FakeSession()
    yield
    app.dependency_overrides.clear()


client = TestClient(app)
body = lambda **kw: {"student_id": "S101", "dob": "2003-05-01", **kw}  # noqa: E731


def test_verify_ok_returns_entry_token_rounds_and_logs():
    r = client.post("/api/v1/entry/verify", json=body(student_id=" s101 "))
    assert r.status_code == 200 and r.json()["candidate"]["full_name"] == "Asha"
    assert r.json()["rounds"][0]["round_id"] == str(RID)
    assert r.json()["rounds"][0]["duration_min"] == 30.0  # drives the exam timer
    assert LOGS == [(CID, RID, "id_dob", True)]


def test_wrong_dob_401_and_logged():
    r = client.post("/api/v1/entry/verify", json=body(dob="2003-05-02"))
    assert r.status_code == 401 and r.json()["error"]["code"] == "ENTRY_MISMATCH"
    assert LOGS == [(CID, RID, "id_dob", False)]


def test_unknown_id_same_401():
    r = client.post("/api/v1/entry/verify", json=body(student_id="S999"))
    assert r.status_code == 401 and r.json()["error"]["code"] == "ENTRY_MISMATCH"
    assert (
        r.json()["error"]["message"]
        == client.post("/api/v1/entry/verify", json=body(dob="1999-01-01")).json()["error"][
            "message"
        ]
    )


def test_lockout_after_repeated_failures():
    FAILS[0] = 5
    r = client.post("/api/v1/entry/verify", json=body())
    assert r.status_code == 429 and LOGS == []


def test_steps_need_entry_token():
    step = {"round_id": str(RID), "step": "terms", "ok": True}
    assert client.post("/api/v1/entry/steps", json=step).status_code == 401
    h = {"Authorization": "Bearer garbage"}
    assert client.post("/api/v1/entry/steps", json=step, headers=h).status_code == 401


def test_steps_logged_and_round_checked():
    tok = client.post("/api/v1/entry/verify", json=body()).json()["entry_token"]
    LOGS.clear()
    h = {"Authorization": f"Bearer {tok}"}
    ok = {"round_id": str(RID), "step": "terms", "ok": True}
    assert client.post("/api/v1/entry/steps", json=ok, headers=h).status_code == 204
    assert LOGS == [(CID, RID, "terms", True)]
    other = {**ok, "round_id": str(uuid.uuid4())}  # a round they are not invited to
    assert client.post("/api/v1/entry/steps", json=other, headers=h).status_code == 403
    assert (
        client.post("/api/v1/entry/steps", json={**ok, "step": "id_dob"}, headers=h).status_code
        == 422
    )


def _auth():
    tok = client.post("/api/v1/entry/verify", json=body()).json()["entry_token"]
    LOGS.clear()
    return {"Authorization": f"Bearer {tok}"}


def test_right_dob_is_logged_for_every_round_wrong_dob_once():
    r2 = {**ROW, "round_id": uuid.uuid4()}
    ROWS[:] = [ROW, r2]
    client.post("/api/v1/entry/verify", json=body())
    assert LOGS == [(CID, RID, "id_dob", True), (CID, r2["round_id"], "id_dob", True)]
    LOGS.clear()
    client.post("/api/v1/entry/verify", json=body(dob="2003-05-02"))
    assert LOGS == [(CID, RID, "id_dob", False)]  # one miss, not one per round


def test_confirm_logs_per_round_and_rejects_wrong_dob():
    h = _auth()
    good = {"round_id": str(RID), "student_id": " s101 ", "dob": "2003-05-01"}
    assert client.post("/api/v1/entry/confirm", json=good, headers=h).status_code == 204
    assert LOGS == [(CID, RID, "id_dob", True)]
    bad = client.post("/api/v1/entry/confirm", json={**good, "dob": "2003-05-02"}, headers=h)
    assert bad.status_code == 401 and LOGS[-1] == (CID, RID, "id_dob", False)
    other_id = client.post("/api/v1/entry/confirm", json={**good, "student_id": "S102"}, headers=h)
    assert other_id.status_code == 401
    elsewhere = {**good, "round_id": str(uuid.uuid4())}
    assert client.post("/api/v1/entry/confirm", json=elsewhere, headers=h).status_code == 403


def test_confirm_locks_out_and_needs_token():
    good = {"round_id": str(RID), "student_id": "S101", "dob": "2003-05-01"}
    assert client.post("/api/v1/entry/confirm", json=good).status_code == 401
    h = _auth()
    FAILS[0] = 5
    r = client.post("/api/v1/entry/confirm", json=good, headers=h)
    assert r.status_code == 429 and LOGS == []


def test_get_steps_returns_latest_and_checks_round():
    h = _auth()
    r = client.get(f"/api/v1/entry/steps?round_id={RID}", headers=h)
    assert r.status_code == 200 and r.json() == {"terms": True, "location": False}
    assert client.get(f"/api/v1/entry/steps?round_id={uuid.uuid4()}", headers=h).status_code == 403
    assert client.get(f"/api/v1/entry/steps?round_id={RID}").status_code == 401

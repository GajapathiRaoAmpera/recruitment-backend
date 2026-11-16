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
CAND = {"candidate_id": CID, "dob": date(2003, 5, 1), "full_name": "Asha"}
ROW = {  # one of the candidate's rounds (repo.candidate_rounds)
    "round_id": RID,
    "round_type": "aptitude",
    "test_id": None,
    "duration_min": 30.0,
    "attempt_status": "not_started",
}
TARGET = {**CAND, "student_id": "S101", "round_type": "aptitude", "test_id": None}
LOGS: list = []
FAILS = [0]
CANDS = [CAND]
ROUNDS: dict = {}  # candidate id -> rounds
LATEST: dict = {}  # candidate id -> round a wrong DOB is logged against (repo.failure_round)


class FakeSession:
    async def commit(self):
        pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    LOGS.clear()
    FAILS[0] = 0
    CANDS[:] = [CAND]
    ROUNDS.clear()
    ROUNDS[CID] = [ROW]
    LATEST.clear()
    LATEST[CID] = RID

    async def find(_, sid):
        return CANDS if sid.strip().lower() == "s101" else []

    async def rounds(_, cid):
        return ROUNDS.get(cid, [])

    async def failure_round(_, cid):
        return LATEST.get(cid)

    async def target(_, cid, rid):
        return TARGET if (cid, rid) == (CID, RID) else None

    async def log(_, cid, rid, step, ok, meta):
        LOGS.append((cid, rid, step, ok))

    async def failures(*_):
        return FAILS[0]

    monkeypatch.setattr(entry.repo, "find_candidates", find)
    monkeypatch.setattr(entry.repo, "candidate_rounds", rounds)
    monkeypatch.setattr(entry.repo, "failure_round", failure_round)
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
    ROUNDS[CID] = [ROW, r2]
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


# ---- B5: sign-in no longer depends on an open round; rounds carry attempt_status -------------
def _row(status, **over):
    return {**ROW, "round_id": uuid.uuid4(), "attempt_status": status, **over}


def test_verify_returns_attempt_status_and_keeps_the_old_fields():
    r = client.post("/api/v1/entry/verify", json=body())
    assert r.json()["rounds"] == [
        {"round_id": str(RID), "type": "aptitude", "test_id": None, "duration_min": 30.0,
         "attempt_status": "not_started"}
    ]  # fmt: skip


def test_finished_student_signs_in_with_no_rounds_instead_of_a_mismatch():
    ROUNDS[CID] = []  # every round done (or HR advanced it): nothing listed
    r = client.post("/api/v1/entry/verify", json=body())
    assert r.status_code == 200 and r.json()["rounds"] == [] and r.json()["entry_token"]
    assert LOGS == []  # nothing to log a right DOB against


def test_submitted_locked_and_expired_rounds_are_listed_but_only_sittable_ones_logged():
    rows = [_row("submitted"), _row("locked"), _row("expired"), _row("in_progress")]
    ROUNDS[CID] = rows
    r = client.post("/api/v1/entry/verify", json=body())
    got = [(x["round_id"], x["attempt_status"]) for x in r.json()["rounds"]]
    assert got == [(str(x["round_id"]), x["attempt_status"]) for x in rows]
    assert LOGS == [(CID, x["round_id"], "id_dob", True) for x in rows[1:]]  # not the submitted


def test_wrong_dob_of_a_finished_student_is_logged_against_their_latest_round():
    ROUNDS[CID] = []
    old = uuid.uuid4()
    LATEST[CID] = old
    r = client.post("/api/v1/entry/verify", json=body(dob="2003-05-02"))
    assert r.status_code == 401 and r.json()["error"]["code"] == "ENTRY_MISMATCH"
    assert LOGS == [(CID, old, "id_dob", False)]


def test_finished_student_is_locked_out_after_repeated_failures():
    ROUNDS[CID] = []
    FAILS[0] = 5
    r = client.post("/api/v1/entry/verify", json=body())
    assert r.status_code == 429 and r.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"


def test_candidate_in_no_round_with_wrong_dob_gets_the_same_401_and_no_log():
    LATEST.clear()  # in no round at all: the log needs a round (FK)
    ROUNDS[CID] = []
    r = client.post("/api/v1/entry/verify", json=body(dob="1999-01-01"))
    unknown = client.post("/api/v1/entry/verify", json=body(student_id="S999"))
    assert r.status_code == unknown.status_code == 401
    assert r.json() == unknown.json() and LOGS == []


def test_same_id_in_two_drives_prefers_the_candidate_with_rounds():
    newer = {**CAND, "candidate_id": uuid.uuid4(), "full_name": "Asha (old drive)"}
    CANDS[:] = [newer, CAND]  # newest first; the newer one has nothing open
    ROUNDS[newer["candidate_id"]] = []
    r = client.post("/api/v1/entry/verify", json=body())
    assert r.json()["candidate"]["full_name"] == "Asha" and len(r.json()["rounds"]) == 1


# ---- B7: GET /entry/rounds ---------------------------------------------------------------------
def test_entry_rounds_is_the_verify_list_and_logs_nothing():
    ROUNDS[CID] = [_row("in_progress"), _row("locked")]
    v = client.post("/api/v1/entry/verify", json=body()).json()
    LOGS.clear()
    r = client.get("/api/v1/entry/rounds", headers={"Authorization": f"Bearer {v['entry_token']}"})
    assert r.status_code == 200 and r.json() == {"rounds": v["rounds"]}
    assert LOGS == []
    ROUNDS[CID][1]["attempt_status"] = "in_progress"  # HR resumed it: the list is fresh
    r = client.get("/api/v1/entry/rounds", headers={"Authorization": f"Bearer {v['entry_token']}"})
    assert [x["attempt_status"] for x in r.json()["rounds"]] == ["in_progress", "in_progress"]


def test_entry_rounds_needs_an_entry_token():
    assert client.get("/api/v1/entry/rounds").status_code == 401
    att = entry.issue_attempt_token(uuid.uuid4(), CID)  # an attempt token is not an entry token
    bad = client.get("/api/v1/entry/rounds", headers={"Authorization": f"Bearer {att}"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "INVALID_TOKEN"

"""GET /rounds/{id}/decisions and POST /rounds/{id}/decide: the AI interview recommends, HR decides.
DB is faked like test_advance_api.py."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.advance import repo, service
from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.main import app

HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
OTHER_HR = StaffUser(id=uuid.uuid4(), email="hr2@x.com", full_name="H R2", role="hr")
DRIVE, ROUND, FINAL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
API = "/api/v1/rounds"
SCORES = {"communication": 4, "technical_depth": 3, "problem_solving": 5, "culture": 4}


def cand(name, interview="completed", scores=SCORES):
    return dict(
        candidate_id=uuid.uuid4(), student_id=f"S-{name}", name=name, status="in_progress",
        interview_id=uuid.uuid4() if interview else None, interview_status=interview,
        ai_scores=scores if interview else None,
    )  # fmt: skip


class FakeSession:
    def __init__(self, state):
        self.state = state

    async def commit(self):
        self.state["commits"] += 1


@pytest.fixture
def db(monkeypatch):
    state = dict(
        round=dict(id=ROUND, drive_id=DRIVE, position=3, type="ai_interview", status="active",
                   threshold=None),
        rows=[cand("Asha"), cand("Ben", scores={"pending": True}), cand("Cara", interview=None)],
        next=dict(id=FINAL, type="final", status="pending"),
        saved=None, completed=False, opened=[], invited=[], closed=[], audits=[], commits=0,
    )  # fmt: skip

    async def get_round(_, rid, owner, lock=False):
        if rid != ROUND or (owner is not None and owner != HR.id):
            return None
        return dict(state["round"])

    async def decision_rows(_, rid):
        return [dict(r) for r in state["rows"]]

    async def save_results(_, rid, rows):
        state["saved"] = rows

    async def complete_round(_, rid, t):
        state["completed"] = True

    async def next_round(_, d, p):
        return state["next"]

    async def open_round(_, rid):
        state["opened"].append(rid)

    async def invite(_, rid, ids):
        state["invited"].append((rid, ids))
        return ids

    async def close_drive(_, d):
        state["closed"].append(d)

    async def audit(_, actor, action, entity, eid, diff=None):
        state["audits"].append(dict(action=action, diff=diff))

    for name, fn in dict(
        get_round=get_round, decision_rows=decision_rows, save_results=save_results,
        complete_round=complete_round, next_round=next_round, open_round=open_round,
        invite=invite, close_drive=close_drive,
    ).items():  # fmt: skip
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(service, "audit", audit)
    app.dependency_overrides[get_session] = lambda: FakeSession(state)
    app.dependency_overrides[security.current_user] = lambda: HR
    yield state
    app.dependency_overrides.clear()


def body(state, *passed):
    return {
        "decisions": [
            {"candidate_id": str(r["candidate_id"]), "decision": "pass" if r["name"] in passed
             else "fail"} for r in state["rows"]
        ]
    }  # fmt: skip


def test_list_shows_ai_average_as_advice(db):
    r = TestClient(app).get(f"{API}/{ROUND}/decisions")
    assert r.status_code == 200
    rows = {c["name"]: c for c in r.json()["candidates"]}
    assert rows["Asha"]["scoring"] == "done" and rows["Asha"]["ai_average"] == 4.0
    assert rows["Ben"]["scoring"] == "pending" and rows["Ben"]["ai_average"] is None
    assert rows["Cara"]["interview_status"] is None and rows["Cara"]["scoring"] is None


def test_hr_decision_wins_over_ai_and_passed_move_to_final(db):
    # Ben has no AI score yet and Asha has a good one: HR passes Ben and fails Asha anyway
    r = TestClient(app).post(f"{API}/{ROUND}/decide", json=body(db, "Ben"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["passed"] == 1 and out["failed"] == 2 and out["drive_closed"] is False
    assert out["next_round_id"] == str(FINAL) and [i["name"] for i in out["invited"]] == ["Ben"]
    status = {s["c"]: s["s"] for s in db["saved"]}
    by_name = {r["name"]: str(r["candidate_id"]) for r in db["rows"]}
    assert status[by_name["Ben"]] == "passed" and status[by_name["Asha"]] == "failed"
    assert {s["c"]: s["score"] for s in db["saved"]}[by_name["Asha"]] == 4.0
    assert db["completed"] and db["opened"] == [FINAL] and db["closed"] == []
    assert db["audits"][0]["action"] == "round.decide" and db["commits"] == 1


def test_final_round_selects_and_closes_the_drive(db):
    db["round"]["type"], db["next"] = "final", None
    r = TestClient(app).post(f"{API}/{ROUND}/decide", json=body(db, "Asha"))
    assert r.status_code == 200 and r.json()["drive_closed"] is True
    assert sorted(s["s"] for s in db["saved"]) == ["rejected", "rejected", "selected"]
    assert db["closed"] == [DRIVE] and db["invited"] == []


def test_every_student_must_be_decided(db):
    b = body(db)
    b["decisions"].pop()
    r = TestClient(app).post(f"{API}/{ROUND}/decide", json=b)
    assert r.status_code == 422 and r.json()["error"]["code"] == "DECISIONS_INCOMPLETE"
    assert r.json()["error"]["details"]["missing"] == [str(db["rows"][-1]["candidate_id"])]
    b["decisions"] += [b["decisions"][0]]
    r = TestClient(app).post(f"{API}/{ROUND}/decide", json=b)
    assert r.json()["error"]["code"] == "DUPLICATE_DECISION"
    assert db["saved"] is None and db["commits"] == 0


def test_live_interview_blocks(db):
    db["rows"][0]["interview_status"] = "live"
    r = TestClient(app).post(f"{API}/{ROUND}/decide", json=body(db))
    assert r.status_code == 409 and r.json()["error"]["code"] == "INTERVIEW_LIVE"
    assert db["saved"] is None


@pytest.mark.parametrize(
    "status,code", [("completed", "ROUND_ALREADY_DECIDED"), ("pending", "ROUND_NOT_ACTIVE")]
)
def test_round_must_be_open(db, status, code):
    db["round"]["status"] = status
    r = TestClient(app).post(f"{API}/{ROUND}/decide", json=body(db))
    assert r.status_code == 409 and r.json()["error"]["code"] == code


def test_test_rounds_use_advance_instead(db):
    db["round"]["type"] = "coding"
    r = TestClient(app).get(f"{API}/{ROUND}/decisions")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROUND_NOT_HR_DECIDED"


def test_other_hr_gets_404(db):
    app.dependency_overrides[security.current_user] = lambda: OTHER_HR
    assert TestClient(app).get(f"{API}/{ROUND}/decisions").status_code == 404
    assert TestClient(app).post(f"{API}/{ROUND}/decide", json=body(db)).status_code == 404


def test_bad_decision_value_is_422(db):
    b = body(db)
    b["decisions"][0]["decision"] = "maybe"
    assert TestClient(app).post(f"{API}/{ROUND}/decide", json=b).status_code == 422

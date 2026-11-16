"""POST /attempts/{id}/resume through the route with the repo faked. The real SQL (deadline math,
counter reset, locking) runs in test_resume_db.py."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.auth.schemas import StaffUser
from app.auth.security import current_user
from app.core.db import get_session
from app.integrity import repo, resume
from app.main import app

HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
OTHER_HR = StaffUser(id=uuid.uuid4(), email="hr2@x.com", full_name="H R2", role="hr")
ADMIN = StaffUser(id=uuid.uuid4(), email="sa@x.com", full_name="S A", role="super_admin")
AID, RID = uuid.uuid4(), uuid.uuid4()
URL = f"/api/v1/attempts/{AID}/resume"
NEW_DEADLINE = datetime(2026, 11, 16, 12, 10, tzinfo=UTC)


class FakeSession:
    def __init__(self, state):
        self.state = state

    async def commit(self):
        self.state["commits"] += 1


@pytest.fixture
def db(monkeypatch):
    state = dict(
        attempt=dict(status="locked", tab_switches=3, remaining_sec_on_lock=600),
        round_status="active", owner=HR.id, resumed=[], audits=[], commits=0, owners=[],
    )  # fmt: skip

    async def find_for_resume(_, aid, owner):
        state["owners"].append(owner)
        if aid != AID or (owner is not None and owner != state["owner"]):
            return None
        return {"id": AID, "round_id": RID}

    async def lock_for_resume(_, aid, rid):
        assert (aid, rid) == (AID, RID)
        return {"round_status": state["round_status"], **state["attempt"]}

    async def do_resume(_, aid):
        a = state["attempt"]
        if a["status"] != "locked" or a["remaining_sec_on_lock"] is None:
            return None
        state["resumed"].append(aid)
        a.update(status="in_progress", tab_switches=0, remaining_sec_on_lock=None)
        return {"deadline_at": NEW_DEADLINE, "tab_switches": 0}

    async def audit(_, actor, action, entity, eid, diff=None):
        state["audits"].append(dict(actor=actor, action=action, entity=entity, id=eid, diff=diff))

    monkeypatch.setattr(repo, "find_for_resume", find_for_resume)
    monkeypatch.setattr(repo, "lock_for_resume", lock_for_resume)
    monkeypatch.setattr(repo, "resume", do_resume)
    monkeypatch.setattr(resume, "audit", audit)
    app.dependency_overrides[get_session] = lambda: FakeSession(state)
    state["user"] = HR
    app.dependency_overrides[current_user] = lambda: state["user"]
    yield state
    app.dependency_overrides.clear()


client = TestClient(app)


def post(reason="Network dropped, confirmed by invigilator"):
    return client.post(URL, json={"reason": reason})


def test_resume_reopens_and_audits(db):
    r = post("  Network dropped  ")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "attempt_id": str(AID),
        "status": "in_progress",
        "deadline_at": NEW_DEADLINE.isoformat().replace("+00:00", "Z"),
        "tab_switches": 0,
    }
    assert db["resumed"] == [AID] and db["commits"] == 1
    (a,) = db["audits"]
    assert (a["actor"], a["action"], a["entity"], a["id"]) == (HR.id, "attempt.resume", "attempt",
                                                              AID)  # fmt: skip
    assert a["diff"] == {
        "reason": "Network dropped",  # trimmed
        "previous_tab_switches": 3,
        "restored_sec": 600,
        "deadline_at": NEW_DEADLINE,
    }
    assert db["owners"] == [HR.id]  # HR is limited to their own drives


def test_other_hr_gets_404_and_nothing_changes(db):
    db["user"] = OTHER_HR
    r = post()
    assert r.status_code == 404 and r.json()["error"]["code"] == "ATTEMPT_NOT_FOUND"
    assert db["resumed"] == [] and db["audits"] == [] and db["commits"] == 0


def test_unknown_attempt_is_404(db):
    r = client.post(f"/api/v1/attempts/{uuid.uuid4()}/resume", json={"reason": "abc"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "ATTEMPT_NOT_FOUND"


def test_super_admin_can_resume_any_drive(db):
    db["user"] = ADMIN
    assert post().status_code == 200 and db["owners"] == [None]


@pytest.mark.parametrize("status", ["in_progress", "submitted", "expired", "not_started"])
def test_only_a_locked_attempt_can_be_resumed(db, status):
    db["attempt"]["status"] = status
    r = post()
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_LOCKED"
    assert db["resumed"] == [] and db["audits"] == [] and db["commits"] == 0


def test_double_resume_is_not_locked(db):
    assert post().status_code == 200
    r = post()
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_LOCKED"
    assert len(db["audits"]) == 1 and db["commits"] == 1


@pytest.mark.parametrize("round_status", ["completed", "pending", "skipped"])
def test_round_not_active_is_refused(db, round_status):
    db["round_status"] = round_status
    r = post()
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROUND_NOT_ACTIVE"
    assert db["resumed"] == [] and db["audits"] == []


def test_missing_remaining_time_is_refused_not_guessed(db):
    db["attempt"]["remaining_sec_on_lock"] = None
    r = post()
    assert r.status_code == 409 and r.json()["error"]["code"] == "REMAINING_TIME_UNKNOWN"
    assert db["resumed"] == [] and db["audits"] == [] and db["commits"] == 0


@pytest.mark.parametrize(
    "payload",
    [{}, {"reason": ""}, {"reason": "  a "}, {"reason": "x" * 501}, {"reason": None},
     {"reason": "fine reason", "extra": 1}],
)  # fmt: skip
def test_bad_reason_is_422(db, payload):
    r = client.post(URL, json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert db["resumed"] == []


def test_reason_limits_are_inclusive(db):
    assert post("abc").status_code == 200
    db["attempt"].update(status="locked", remaining_sec_on_lock=5)
    assert post("x" * 500).status_code == 200


def test_resume_needs_staff_login():
    app.dependency_overrides.clear()
    r = client.post(URL, json={"reason": "abc"})
    assert r.status_code == 401


def test_a_student_attempt_token_is_not_staff_auth():
    from app.auth import entry

    app.dependency_overrides.clear()
    tok = entry.issue_attempt_token(AID, uuid.uuid4())
    r = client.post(URL, json={"reason": "abc"}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401

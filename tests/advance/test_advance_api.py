"""GET /rounds/{id}/ranking and POST /rounds/{id}/advance through the routes. DB is faked; the
real SQL runs in test_advance_db.py."""

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.advance import repo, service
from app.attempts import state_repo
from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.main import app

HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
OTHER_HR = StaffUser(id=uuid.uuid4(), email="hr2@x.com", full_name="H R2", role="hr")
ADMIN = StaffUser(id=uuid.uuid4(), email="sa@x.com", full_name="S A", role="super_admin")
DRIVE, ROUND, NEXT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
API = "/api/v1/rounds"


def cand(name, status="submitted", marks=None, max_marks=10, secs=100):
    return dict(
        candidate_id=uuid.uuid4(),
        student_id=f"S-{name}",
        name=name,
        rc_status="in_progress" if status else "invited",
        attempt_id=uuid.uuid4() if status else None,
        attempt_status=status,
        marks=None if marks is None else Decimal(str(marks)),
        max_marks=None if status is None else Decimal(str(max_marks)),
        time_taken_sec=secs if status == "submitted" else None,
    )


class FakeSession:
    def __init__(self, state):
        self.state = state

    async def commit(self):
        self.state["commits"] += 1


@pytest.fixture
def db(monkeypatch):
    state = dict(
        round=dict(
            id=ROUND, drive_id=DRIVE, position=1, type="aptitude", status="active",
            threshold=Decimal("60.00"),
        ),
        rows=[cand("Asha", marks=8), cand("Ben", marks=5), cand("Cara", marks=3),
              cand("Dev", status=None)],
        expired=[], next=dict(id=NEXT, type="coding", status="pending"), already=set(),
        submitted=[], reopened=[], saved=None, completed=None, opened=[], invited=[],
        audits=[], commits=0, locks=[],
    )  # fmt: skip

    async def get_round(_, rid, owner, lock=False):
        state["locks"].append(lock)
        if rid != ROUND or (owner is not None and owner != HR.id):
            return None
        return dict(state["round"])

    async def attempts_past_deadline(_, rid):
        return list(state["expired"])

    async def submit(_, aid):  # the real one lives in app/attempts/state_repo.py
        state["submitted"].append(aid)
        for r in state["rows"]:
            if r["attempt_id"] == aid and r["attempt_status"] == "in_progress":
                r["attempt_status"] = "submitted"
                r["marks"] = r["marks"] or Decimal(0)
        return {"time_taken_sec": 1}

    async def reopen_locked(_, ids):
        state["reopened"].extend(ids)
        for r in state["rows"]:
            if r["attempt_id"] in ids:
                r["attempt_status"] = "in_progress"

    async def round_rows(_, rid):
        return [dict(r) for r in state["rows"]]

    async def save_results(_, rid, rows):
        state["saved"] = rows

    async def complete_round(_, rid, t):
        state["completed"] = t
        state["round"]["status"] = "completed"

    async def next_round(_, d, p):
        return state["next"]

    async def open_round(_, rid):
        state["opened"].append(rid)

    async def invite(_, rid, ids):
        state["invited"].append((rid, ids))
        return [i for i in ids if i not in state["already"]]

    async def audit(_, actor, action, entity, eid, diff=None):
        state["audits"].append(dict(actor=actor, action=action, entity=entity, id=eid, diff=diff))

    for name, fn in dict(
        get_round=get_round, attempts_past_deadline=attempts_past_deadline,
        reopen_locked=reopen_locked, round_rows=round_rows, save_results=save_results,
        complete_round=complete_round, next_round=next_round, open_round=open_round,
        invite=invite,
    ).items():  # fmt: skip
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(state_repo, "submit", submit)
    monkeypatch.setattr(service, "audit", audit)
    app.dependency_overrides[get_session] = lambda: FakeSession(state)
    yield state
    app.dependency_overrides.clear()


def as_user(user):
    app.dependency_overrides[security.current_user] = lambda: user
    return TestClient(app)


def names(state, status):
    return {r["name"] for r in state["rows"] for s in state["saved"] if s["c"] == str(
        r["candidate_id"]) and s["s"] == status}  # fmt: skip


# ---- auth and ownership ---------------------------------------------------------------------


def test_both_routes_need_a_staff_login(db):
    c = TestClient(app)
    assert c.get(f"{API}/{ROUND}/ranking").status_code == 401
    assert c.post(f"{API}/{ROUND}/advance", json={"threshold": 50}).status_code == 401


def test_other_hr_and_unknown_round_get_404(db):
    for user, rid in ((OTHER_HR, ROUND), (HR, uuid.uuid4())):
        r = as_user(user).get(f"{API}/{rid}/ranking")
        assert r.status_code == 404 and r.json()["error"]["code"] == "ROUND_NOT_FOUND"
        r = as_user(user).post(f"{API}/{rid}/advance", json={"threshold": 50})
        assert r.status_code == 404 and r.json()["error"]["code"] == "ROUND_NOT_FOUND"
    assert db["saved"] is None and db["audits"] == [] and db["commits"] == 0


def test_super_admin_sees_every_drive(db):
    assert as_user(ADMIN).get(f"{API}/{ROUND}/ranking").status_code == 200
    assert as_user(ADMIN).post(f"{API}/{ROUND}/advance", json={"threshold": 50}).status_code == 200
    assert db["audits"][0]["actor"] == ADMIN.id


def test_interview_round_is_not_ranked(db):
    db["round"]["type"] = "ai_interview"
    r = as_user(HR).get(f"{API}/{ROUND}/ranking")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROUND_HAS_NO_TEST"
    r = as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 50})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROUND_HAS_NO_TEST"


@pytest.mark.parametrize(
    "body", [{"threshold": 101}, {"threshold": -1}, {}, {"threshold": 50, "x": 1}]
)
def test_bad_body_is_422(db, body):
    r = as_user(HR).post(f"{API}/{ROUND}/advance", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_threshold_query_out_of_range_is_422(db):
    assert as_user(HR).get(f"{API}/{ROUND}/ranking?threshold=120").status_code == 422


# ---- preview --------------------------------------------------------------------------------


def test_preview_uses_saved_threshold_and_the_query_overrides_it(db):
    out = as_user(HR).get(f"{API}/{ROUND}/ranking").json()
    assert out["threshold"] == 60.0 and out["round_status"] == "active"
    assert out["counts"] == {"pass": 1, "fail": 2, "absent": 1, "still_writing": 0, "locked": 0}
    asha = out["candidates"][0]
    keys = ("name", "marks", "max_marks", "percent", "rank", "would_pass")
    assert {k: asha[k] for k in keys} == {
        "name": "Asha", "marks": 8.0, "max_marks": 10.0, "percent": 80.0, "rank": 1,
        "would_pass": True,
    }  # fmt: skip
    assert set(asha) == {
        "candidate_id", "student_id", "name", "marks", "max_marks", "percent", "rank",
        "time_taken_sec", "would_pass", "attempt_status",
    }  # fmt: skip
    out = as_user(HR).get(f"{API}/{ROUND}/ranking?threshold=30").json()
    assert out["threshold"] == 30.0 and out["counts"]["pass"] == 3
    assert db["saved"] is None and db["audits"] == []  # the preview changes nothing


def test_preview_with_no_threshold_anywhere(db):
    db["round"]["threshold"] = None
    out = as_user(HR).get(f"{API}/{ROUND}/ranking").json()
    assert out["threshold"] is None and out["counts"]["pass"] is None
    assert {c["would_pass"] for c in out["candidates"]} == {None}


def test_preview_finalizes_past_deadline_attempts_first(db):
    late = cand("Late", status="in_progress", marks=9)
    db["rows"].append(late)
    db["expired"] = [late["attempt_id"]]
    out = as_user(HR).get(f"{API}/{ROUND}/ranking").json()
    assert db["submitted"] == [late["attempt_id"]] and db["commits"] == 1
    assert out["counts"]["still_writing"] == 0 and out["candidates"][0]["name"] == "Late"


def test_preview_of_a_completed_round_writes_nothing(db):
    db["round"]["status"] = "completed"
    db["expired"] = [uuid.uuid4()]
    assert as_user(HR).get(f"{API}/{ROUND}/ranking").status_code == 200
    assert db["submitted"] == [] and db["commits"] == 0


# ---- advance --------------------------------------------------------------------------------


def test_advance_marks_results_completes_and_invites_the_passed(db, no_candidate_mail):
    r = as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 60})
    assert r.status_code == 200, r.text
    out = r.json()
    asha = db["rows"][0]
    assert out == {
        "round_id": str(ROUND), "threshold": 60.0, "passed": 1, "failed": 2, "absent": 1,
        "next_round_id": str(NEXT),
        "invited": [{"candidate_id": str(asha["candidate_id"]), "student_id": "S-Asha",
                     "name": "Asha"}],
    }  # fmt: skip
    assert db["locks"] == [True]  # the round row is locked for the whole transaction
    assert no_candidate_mail == [(NEXT, [asha["candidate_id"]], False)]  # passed are emailed
    assert names(db, "passed") == {"Asha"}
    assert names(db, "failed") == {"Ben", "Cara"}
    assert names(db, "absent") == {"Dev"}
    by_c = {s["c"]: s for s in db["saved"]}
    assert by_c[str(asha["candidate_id"])]["score"] == Decimal("80.00")
    assert by_c[str(asha["candidate_id"])]["rank"] == 1
    dev = by_c[str(db["rows"][3]["candidate_id"])]
    assert dev["score"] is None and dev["rank"] is None
    assert db["completed"] == Decimal("60.00") and db["opened"] == [NEXT]
    assert db["invited"] == [(NEXT, [asha["candidate_id"]])]
    assert db["commits"] == 1
    (a,) = db["audits"]
    assert (a["actor"], a["action"], a["entity"], a["id"]) == (
        HR.id,
        "round.advance",
        "round",
        ROUND,
    )
    assert a["diff"]["passed"] == 1 and a["diff"]["next_round_id"] == NEXT
    assert a["diff"]["invited"] == [asha["candidate_id"]]


def test_already_invited_students_are_not_listed_twice(db):
    db["already"] = {db["rows"][0]["candidate_id"]}
    out = as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 60}).json()
    assert out["passed"] == 1 and out["invited"] == []


def test_last_round_just_finishes(db):
    db["next"] = None
    out = as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 60}).json()
    assert out["next_round_id"] is None and out["invited"] == []
    assert db["opened"] == [] and db["invited"] == [] and db["completed"] == Decimal("60.00")
    assert db["commits"] == 1


@pytest.mark.parametrize(
    "status, code",
    [("completed", "ROUND_ALREADY_ADVANCED"), ("pending", "ROUND_NOT_ACTIVE"),
     ("skipped", "ROUND_NOT_ACTIVE")],
)  # fmt: skip
def test_only_an_active_round_advances(db, status, code):
    db["round"]["status"] = status
    r = as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 60})
    assert r.status_code == 409 and r.json()["error"]["code"] == code
    assert db["saved"] is None and db["commits"] == 0 and db["audits"] == []


def test_second_advance_is_refused(db):
    assert as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 60}).status_code == 200
    r = as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 60})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROUND_ALREADY_ADVANCED"
    assert db["commits"] == 1 and len(db["audits"]) == 1


def test_student_still_writing_blocks_advance(db):
    db["rows"].append(cand("Busy", status="in_progress", marks=None))
    r = as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 60})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "ROUND_STILL_RUNNING"
    assert err["details"]["counts"] == {
        "pass": 1, "fail": 2, "absent": 1, "still_writing": 1, "locked": 0,
    }  # fmt: skip
    assert db["saved"] is None and db["commits"] == 0 and db["audits"] == []


def test_past_deadline_attempts_are_finalized_then_ranked(db):
    late = cand("Late", status="in_progress", marks=7)
    db["rows"].append(late)
    db["expired"] = [late["attempt_id"]]
    out = as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 60}).json()
    assert db["submitted"] == [late["attempt_id"]]
    assert out["passed"] == 2 and names(db, "passed") == {"Asha", "Late"}
    assert db["audits"][0]["diff"]["finalized_expired"] == [late["attempt_id"]]


def test_locked_attempt_blocks_advance_with_the_list(db):
    lock = cand("Lock", status="locked", marks=None)
    db["rows"].append(lock)
    r = as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 60})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "LOCKED_ATTEMPTS"
    assert err["details"]["attempts"] == [
        {"attempt_id": str(lock["attempt_id"]), "candidate_id": str(lock["candidate_id"]),
         "student_id": "S-Lock", "name": "Lock"}
    ]  # fmt: skip
    assert db["reopened"] == [] and db["saved"] is None and db["commits"] == 0


def test_finalize_locked_submits_them_through_the_normal_submit(db):
    lock = cand("Lock", status="locked", marks=9)
    db["rows"].append(lock)
    r = as_user(HR).post(f"{API}/{ROUND}/advance", json={"threshold": 60, "finalize_locked": True})
    assert r.status_code == 200, r.text
    assert db["reopened"] == [lock["attempt_id"]] and db["submitted"] == [lock["attempt_id"]]
    assert names(db, "passed") == {"Asha", "Lock"}
    assert db["audits"][0]["diff"]["finalized_locked"] == [lock["attempt_id"]]

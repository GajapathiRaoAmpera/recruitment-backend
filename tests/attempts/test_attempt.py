"""Phase 1: /entry/start, GET /attempt, answers and violations (AC-3, AC-7)."""

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.attempts import state_repo as arepo
from app.attempts import state_service as service
from app.auth import entry as auth_entry
from app.core.config import get_settings
from app.core.db import get_session
from app.integrity import repo as irepo
from app.main import app

QUESTIONS = [
    {"id": uuid.uuid4(), "body": f"Q{i}?", "options": ["a", "b", "c", "d"], "answer": 1}
    for i in range(3)
]

CID, RID, AID, TID = (uuid.uuid4() for _ in range(4))
NOW = datetime.now(UTC)
STATE = {"status": "in_progress", "tab_switches": 0}
CALLS: dict = {}


class FakeSession:
    async def commit(self):
        CALLS["commit"] = CALLS.get("commit", 0) + 1


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    CALLS.clear()
    STATE.clear()
    STATE.update(status="in_progress", tab_switches=0)

    async def get_state(_, a, c):
        if (a, c) != (AID, CID):
            return None
        return {
            "id": AID,
            "test_id": TID,
            "status": STATE["status"],
            "deadline_at": NOW + timedelta(minutes=45),
            "server_now": NOW,
            "tab_switches": STATE["tab_switches"],
            "remaining_sec_on_lock": None,
            "past_deadline": STATE.get("past", False),
        }

    async def qs(_, t):
        return [
            {"id": q["id"], "body": q["body"], "options": q["options"], "marks": 1}
            for q in QUESTIONS
        ]

    async def coding(*_):
        return STATE.get("coding", [])

    async def submit_fake(_, a):
        CALLS["auto_submitted"] = CALLS.get("auto_submitted", 0) + 1
        return {"time_taken_sec": 60}

    async def saved(*_):
        return {str(QUESTIONS[0]["id"]): 2}

    async def save_answer(_, a, q, r):
        return STATE.get("answer", "ok")

    async def tab_switch(_, a, mx):
        if STATE["status"] != "in_progress":
            return None
        STATE["tab_switches"] += 1
        if STATE["tab_switches"] >= mx:
            STATE["status"] = "locked"
        return {"tab_switches": STATE["tab_switches"], "status": STATE["status"]}

    async def current(*_):
        return {"tab_switches": STATE["tab_switches"], "status": STATE["status"]}

    async def log_violation(_, a, kind):
        CALLS.setdefault("violations", []).append(kind)

    for mod, name, fn in [
        (arepo, "get_state", get_state),
        (arepo, "coding_questions_for", coding),
        (arepo, "submit", submit_fake),
        (arepo, "questions_for", qs),
        (arepo, "saved_answers", saved),
        (arepo, "save_answer", save_answer),
        (irepo, "tab_switch", tab_switch),
        (irepo, "current", current),
        (irepo, "log_violation", log_violation),
    ]:
        monkeypatch.setattr(mod, name, fn)
    app.dependency_overrides[get_session] = lambda: FakeSession()
    yield
    app.dependency_overrides.clear()


client = TestClient(app)
entry = lambda: {"Authorization": "Bearer " + _entry()}  # noqa: E731
attempt = lambda: {"Authorization": "Bearer " + auth_entry.issue_attempt_token(AID, CID)}  # noqa: E731


def _entry():
    import jwt

    return jwt.encode(
        {"typ": "entry", "cid": str(CID), "exp": time.time() + 600},
        get_settings().entry_signing_key,
        algorithm="HS256",
    )


def test_token_types_are_not_interchangeable():
    entry_token = _entry()
    assert (
        client.get(
            "/api/v1/attempt", headers={"Authorization": f"Bearer {entry_token}"}
        ).status_code
        == 401
    )
    att = auth_entry.issue_attempt_token(AID, CID)
    assert (
        client.post(
            "/api/v1/entry/start",
            json={"round_id": str(RID)},
            headers={"Authorization": f"Bearer {att}"},
        ).status_code
        == 401
    )
    assert client.get("/api/v1/attempt").status_code == 401
    other = auth_entry.issue_attempt_token(
        uuid.uuid4(), CID
    )  # attempt that isn't theirs / doesn't exist
    assert (
        client.get("/api/v1/attempt", headers={"Authorization": f"Bearer {other}"}).status_code
        == 404
    )


def test_attempt_state_has_server_clock_and_never_the_answer_key():
    r = client.get("/api/v1/attempt", headers=attempt())
    body = r.json()
    assert (
        r.status_code == 200
        and len(body["questions"]) == len(QUESTIONS)
        and body["max_tab_switches"] == 3
    )
    assert body["deadline_at"] > body["server_now"] and body["saved_answers"]
    assert set(body["questions"][0]) == {"kind", "id", "body", "options", "marks"}
    assert all("answer" not in q for q in body["questions"])  # keys never leave the server
    assert "correct" not in r.text


CODING = {
    "id": uuid.uuid4(),
    "body": "Two Sum\nGiven numbers, print the indices of the pair that adds to the target.",
    "marks": 10,
    "samples": [{"input": f"{i}", "expected": f"{i}"} for i in range(3)],
}


def test_coding_question_has_title_statement_languages_and_only_sample_cases():
    STATE["coding"] = [CODING]
    body = client.get("/api/v1/attempt", headers=attempt()).json()
    q = next(x for x in body["questions"] if x["kind"] == "coding")
    assert set(q) == {"kind", "id", "title", "statement", "marks", "languages", "samples"}
    assert q["title"] == "Two Sum" and q["statement"].startswith("Two Sum")
    assert {"key": "python", "name": "Python 3.8"} in q["languages"]
    assert len(q["samples"]) == 3 and set(q["samples"][0]) == {"input", "expected"}
    assert all(x["kind"] == "mcq" for x in body["questions"] if x is not q and "options" in x)


def test_a_passed_deadline_auto_submits_once_and_reports_expired():
    STATE["past"] = True
    body = client.get("/api/v1/attempt", headers=attempt()).json()
    assert body["status"] == "expired" and CALLS["auto_submitted"] == 1


def test_answer_outcomes():
    q = str(QUESTIONS[0]["id"])

    def put(v):
        return client.put(f"/api/v1/attempt/answers/{q}", json={"response": v}, headers=attempt())

    assert put(1).status_code == 204
    for outcome, code, status in [
        ("bad_option", "BAD_OPTION", 422),
        ("closed", "ATTEMPT_LOCKED", 409),
        ("expired", "ATTEMPT_EXPIRED", 409),
        ("no_question", "QUESTION_NOT_FOUND", 404),
    ]:
        STATE["answer"] = outcome
        r = put(1)
        assert (r.status_code, r.json()["error"]["code"]) == (status, code)
    assert put(-1).status_code == 422


def test_third_tab_switch_locks_and_later_ones_do_not_count():
    def post(kind="tab_switch"):
        return client.post(
            "/api/v1/attempt/violations", json={"kind": kind}, headers=attempt()
        ).json()

    assert post() == {"tab_switches": 1, "locked": False}
    assert post("fullscreen_exit") == {"tab_switches": 1, "locked": False}  # logged, not counted
    assert post() == {"tab_switches": 2, "locked": False}
    assert post() == {"tab_switches": 3, "locked": True}
    assert post() == {"tab_switches": 3, "locked": True}
    assert (
        CALLS["violations"].count("tab_switch") == 4
        and CALLS["violations"].count("fullscreen_exit") == 1
    )


def test_bad_violation_kind_and_no_token():
    assert (
        client.post("/api/v1/attempt/violations", json={"kind": "x"}, headers=attempt()).status_code
        == 422
    )
    assert client.post("/api/v1/attempt/violations", json={"kind": "tab_switch"}).status_code == 401


def test_service_constant_matches_integrity_rule():
    assert service.MAX_TAB_SWITCHES == 3


# --- submit (finalizes the answers) ---
def _submit_with(monkeypatch, done, current=None):
    async def submit(*_):
        return done

    async def status(*_):
        return current

    monkeypatch.setattr(arepo, "submit", submit)
    monkeypatch.setattr(arepo, "current_status", status)
    return client.post("/api/v1/attempt/submit", headers=attempt())


def test_submit_finalizes_and_returns_time_but_never_the_marks(monkeypatch):
    r = _submit_with(monkeypatch, {"time_taken_sec": 600, "submitted_at": NOW})
    assert r.status_code == 200 and r.json() == {"status": "submitted", "time_taken_sec": 600}
    assert CALLS["commit"] == 1


def test_submit_twice_is_harmless(monkeypatch):
    r = _submit_with(monkeypatch, None, {"status": "submitted", "time_taken_sec": 600})
    assert r.status_code == 200 and r.json()["time_taken_sec"] == 600 and "commit" not in CALLS


def test_locked_attempt_cannot_submit(monkeypatch):
    r = _submit_with(monkeypatch, None, {"status": "locked", "time_taken_sec": None})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ATTEMPT_LOCKED"


def test_submit_needs_the_attempt_token_and_a_real_attempt(monkeypatch):
    assert client.post("/api/v1/attempt/submit").status_code == 401
    assert (
        client.post(
            "/api/v1/attempt/submit", headers={"Authorization": f"Bearer {_entry()}"}
        ).status_code
        == 401
    )
    assert _submit_with(monkeypatch, None, None).status_code == 404

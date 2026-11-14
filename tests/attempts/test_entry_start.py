"""POST /entry/start, the attempt token and the `current_attempt` / `student_attempt` dependencies.

Everything is mocked (no database, no network).
"""

import logging
import time
import uuid

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.attempts import repo
from app.auth import entry
from app.auth import repo as auth_repo
from app.core.config import get_settings
from app.core.db import get_session
from app.core.errors import install_error_handlers
from app.execution.deps import AttemptContext, student_attempt
from app.main import app

CID, RID, AID, TID = (uuid.uuid4() for _ in range(4))
CALLS: dict = {}
URL = "/api/v1/entry/start"


class FakeSession:
    async def commit(self):
        CALLS["commit"] = CALLS.get("commit", 0) + 1


def _jwt(**claims):
    return jwt.encode(claims, get_settings().entry_signing_key, algorithm="HS256")


def entry_token(**over):
    claims = {"typ": "entry", "cid": str(CID), "exp": time.time() + 600, **over}
    return _jwt(**claims)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    CALLS.clear()
    CALLS.update(
        missing=[], existing=None, invited=True, open=True, has_questions=True, paper=None,
        type="aptitude", coding=0,
    )  # fmt: skip

    async def invite(_, c, r):
        return {"candidate_id": c} if CALLS["invited"] and (c, r) == (CID, RID) else None

    async def startable(_, c, r):
        return {"id": RID, "type": CALLS["type"], "minutes": 45.0} if CALLS["open"] else None

    async def find(*_):
        return CALLS["existing"]

    async def missing(*_):
        return list(CALLS["missing"])

    async def ensure_test(_, r, minutes):
        CALLS["minutes"] = minutes
        return {"id": TID, "duration_sec": 2700}

    async def start_attempt(*args):
        CALLS["started"] = args
        return AID

    monkeypatch.setattr(auth_repo, "get_invite_target", invite)
    monkeypatch.setattr(repo, "get_startable", startable)
    monkeypatch.setattr(repo, "find_attempt", find)
    monkeypatch.setattr(repo, "missing_steps", missing)
    monkeypatch.setattr(repo, "ensure_test", ensure_test)

    async def has_questions(*_):
        return CALLS["has_questions"]

    async def sent_paper(*_):
        return CALLS["paper"]

    async def link(_, attempt_id, rows):
        CALLS["linked"] = (attempt_id, rows)

    async def coding_question_count(_, r):
        assert r == RID
        return CALLS["coding"]

    monkeypatch.setattr(repo, "coding_question_count", coding_question_count)
    monkeypatch.setattr(repo, "has_questions", has_questions)
    monkeypatch.setattr(repo, "sent_paper", sent_paper)
    monkeypatch.setattr(repo, "link_questions", link)
    monkeypatch.setattr(repo, "start_attempt", start_attempt)
    app.dependency_overrides[get_session] = lambda: FakeSession()
    yield
    app.dependency_overrides.clear()


client = TestClient(app)


def start(rid=RID, token=None):
    return client.post(URL, json={"round_id": str(rid)}, headers=bearer(token or entry_token()))


# ---- success -------------------------------------------------------------------------------
def test_start_returns_an_attempt_token_with_the_right_claims():
    r = start()
    assert r.status_code == 200 and r.json()["attempt_id"] == str(AID)
    claims = jwt.decode(
        r.json()["attempt_token"], get_settings().entry_signing_key, algorithms=["HS256"]
    )
    assert claims["typ"] == "attempt" and claims["aid"] == str(AID) and claims["cid"] == str(CID)
    assert 2 * 3600 - 60 < claims["exp"] - time.time() <= 2 * 3600  # 2 h
    assert CALLS["minutes"] == 45.0 and CALLS["started"][3:] == (RID, 2700)  # round id, seconds


def test_calling_start_again_on_a_live_attempt_returns_it_again():
    CALLS["existing"] = {"id": AID, "status": "in_progress", "past_deadline": False}
    r = start()
    assert r.status_code == 200 and r.json()["attempt_id"] == str(AID)


# ---- auth ----------------------------------------------------------------------------------
def test_no_token_is_401():
    r = client.post(URL, json={"round_id": str(RID)})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_an_attempt_token_is_not_an_entry_token():
    att = entry.issue_attempt_token(AID, CID)
    r = start(token=att)
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.parametrize("bad", ["garbage", entry_token(exp=time.time() - 5)])
def test_bad_or_expired_entry_token_is_401(bad):
    assert start(token=bad).status_code == 401


# ---- the refusals --------------------------------------------------------------------------
def test_not_invited_is_403():
    CALLS["invited"] = False
    r = start()
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_INVITED"
    assert "started" not in CALLS


def test_unknown_round_is_403():
    assert start(rid=uuid.uuid4()).status_code == 403


def test_invited_but_round_not_open_is_409_with_a_clear_message():
    CALLS["open"] = False
    r = start()
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROUND_NOT_OPEN"
    assert "can't be started" in r.json()["error"]["message"] and "started" not in CALLS


def test_location_and_terms_must_be_logged_ok():
    CALLS["missing"] = ["location", "terms", "fullscreen"]
    r = start()
    assert r.status_code == 409 and r.json()["error"]["code"] == "STEPS_MISSING"
    assert r.json()["error"]["details"]["missing"] == ["location", "terms", "fullscreen"]
    assert "started" not in CALLS and "commit" not in CALLS  # nothing was created


@pytest.mark.parametrize(
    "status,past,code",
    [
        ("submitted", False, "ATTEMPT_LOCKED"),
        ("locked", False, "ATTEMPT_LOCKED"),
        ("expired", False, "ATTEMPT_EXPIRED"),
        ("in_progress", True, "ATTEMPT_EXPIRED"),  # deadline passed, never marked expired
    ],
)
def test_a_finished_attempt_is_never_restarted_and_gets_no_token(status, past, code):
    CALLS["existing"] = {"id": AID, "status": status, "past_deadline": past}
    r = start()
    assert r.status_code == 409 and r.json()["error"]["code"] == code
    assert "attempt_token" not in r.text
    assert "started" not in CALLS and "commit" not in CALLS


def test_no_token_or_dob_is_ever_logged(caplog):
    caplog.set_level(logging.DEBUG)
    token = entry_token()
    got = start(token=token).json()["attempt_token"]
    assert token not in caplog.text and got not in caplog.text


# ---- current_attempt / student_attempt -------------------------------------------------------
def make_probe_app():
    probe = FastAPI()
    install_error_handlers(probe)

    @probe.get("/who")
    async def who(ids: tuple = Depends(entry.current_attempt)):
        return {"attempt_id": str(ids[0]), "candidate_id": str(ids[1])}

    @probe.get("/ctx")
    async def ctx(c: AttemptContext = Depends(student_attempt)):
        return {"attempt_id": str(c.attempt_id), "candidate_id": str(c.candidate_id)}

    return TestClient(probe)


probe = make_probe_app()


@pytest.mark.parametrize("path", ["/who", "/ctx"])
def test_current_attempt_and_student_attempt_return_the_ids(path):
    r = probe.get(path, headers=bearer(entry.issue_attempt_token(AID, CID)))
    assert r.status_code == 200
    assert r.json() == {"attempt_id": str(AID), "candidate_id": str(CID)}


@pytest.mark.parametrize("path", ["/who", "/ctx"])
def test_the_attempt_dependency_rejects_everything_else(path):
    assert probe.get(path).status_code == 401  # no token
    assert probe.get(path, headers=bearer(entry_token())).status_code == 401  # entry token
    assert probe.get(path, headers=bearer("garbage")).status_code == 401
    expired = _jwt(typ="attempt", aid=str(AID), cid=str(CID), exp=time.time() - 5)
    r = probe.get(path, headers=bearer(expired))
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_TOKEN"
    wrong_key = jwt.encode(
        {"typ": "attempt", "aid": str(AID), "cid": str(CID), "exp": time.time() + 60},
        "another-secret-of-sufficient-length-0123456789",
        algorithm="HS256",
    )
    assert probe.get(path, headers=bearer(wrong_key)).status_code == 401
    no_aid = _jwt(typ="attempt", cid=str(CID), exp=time.time() + 60)
    assert probe.get(path, headers=bearer(no_aid)).status_code == 401


# ---- the SQL that keeps the deadline fixed -----------------------------------------------------
class RecordingSession:
    def __init__(self):
        self.sql: list[str] = []

    async def execute(self, stmt, params=None):
        self.sql.append(" ".join(str(stmt).split()))

        class Result:
            def scalar_one(self):
                return AID

        return Result()


@pytest.mark.anyio
async def test_start_attempt_only_fills_in_a_never_started_row(monkeypatch):
    monkeypatch.undo()  # use the real repo function here
    s = RecordingSession()
    assert await repo.start_attempt(s, TID, CID, RID, 2700) == AID
    insert = s.sql[0]
    assert "on conflict (test_id, candidate_id) do update" in insert
    assert "where attempts.status = 'not_started'" in insert  # a started row keeps its deadline


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---- the round's questions come from the paper HR accepted and sent --------------------------
def _paper(n=3):
    pool = [
        {"id": str(uuid.uuid4()), "level": "easy", "section": "aptitude", "topic": "t",
         "style": "aptitude", "text": f"Question {i}?", "options": ["a", "b", "c", "d"],
         "answer": 1, "why": "w", "selected": i != 0}
        for i in range(n)
    ]  # fmt: skip
    settings = {"track": "non_it", "counts": {"easy": 2, "medium": 0, "advanced": 0},
                "mix": {"aptitude": 100, "reasoning": 0, "technical": 0}}  # fmt: skip
    return {"drive_ref": "d1", "settings": settings, "pool": pool}


def test_each_student_gets_their_own_questions_and_option_order():
    CALLS.update(has_questions=False, paper=_paper(6))
    CALLS["paper"]["settings"]["counts"]["easy"] = 5
    assert start().status_code == 200
    aid, rows = CALLS["linked"]
    pool = {q["id"]: q for q in CALLS["paper"]["pool"]}
    assert aid == AID and len(rows) == 5 and rows[0]["p"] == 0
    assert all(r["q"] in pool and pool[r["q"]]["selected"] for r in rows)  # unselected are left out
    for r in rows:  # option order is a permutation of the stored options
        assert sorted(r["o"]) == [0, 1, 2, 3]
    # another student: same seed rule but their own id -> different paper
    CALLS.pop("linked")
    other = ensure_for(uuid.uuid4())
    assert [r["q"] for r in other] != [r["q"] for r in rows] or [r["o"] for r in other] != [
        r["o"] for r in rows
    ]


def ensure_for(candidate_id):
    import asyncio

    from app.attempts import service

    asyncio.run(service.ensure_questions(FakeSession(), AID, candidate_id, RID, "aptitude"))
    return CALLS["linked"][1]


def test_start_refused_when_no_paper_was_sent_to_the_student():
    CALLS.update(has_questions=False, paper=None)
    r = start()
    assert r.status_code == 409 and r.json()["error"]["code"] == "PAPER_NOT_READY"
    assert "linked" not in CALLS and "commit" not in CALLS  # nothing saved, attempt rolled back


def test_coding_round_with_no_questions_is_not_ready_and_one_with_questions_starts():
    CALLS.update(type="coding", coding=0)
    r = start()
    assert r.status_code == 409 and r.json()["error"]["code"] == "PAPER_NOT_READY"
    assert "commit" not in CALLS  # the empty attempt is rolled back
    CALLS.update(coding=2)
    assert start().status_code == 200


def test_sent_paper_decodes_only_the_json_columns(monkeypatch):
    import asyncio

    monkeypatch.undo()  # the autouse fixture replaced repo.sent_paper with a fake

    class R:
        def mappings(self):
            return self

        def one_or_none(self):
            return {
                "drive_ref": "8b17f471-187c-4111-b662-d0e788b91b50",
                "settings": "{}",
                "pool": "[]",
            }

    class S:
        async def execute(self, *_):
            return R()

    got = asyncio.run(repo.sent_paper(S(), RID, CID))
    assert got["drive_ref"].startswith("8b17f471") and got["settings"] == {} and got["pool"] == []

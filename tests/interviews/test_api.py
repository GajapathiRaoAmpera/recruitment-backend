"""Round 3 AI interview routes: standalone sign-in, session, transcript, frames and HR views."""

import time
import uuid
from datetime import UTC, date, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth import repo as auth_repo
from app.auth.schemas import StaffUser
from app.auth.security import current_user
from app.core import audit as audit_mod
from app.core.config import get_settings
from app.core.db import get_session
from app.interviews import frames, gemini, repo, scoring, service, tokens
from app.main import app

CID, RID, IID, DRIVE = (uuid.uuid4() for _ in range(4))
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
DOB = date(2004, 5, 1)
HR = StaffUser(id=uuid.uuid4(), email="h@x.com", full_name="H", role="hr")
ADMIN = StaffUser(id=uuid.uuid4(), email="a@x.com", full_name="A", role="super_admin")
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
S: dict = {}
CALLS: dict = {}


class FakeSession:
    async def commit(self):
        CALLS["commit"] = CALLS.get("commit", 0) + 1


def ctx(**kw):
    started = S.get("started_at")
    return {
        "id": IID,
        "status": S["status"],
        "started_at": started,
        "round_id": RID,
        "candidate_id": CID,
        "minutes": 40,
        "deadline_at": started + timedelta(minutes=40) if started else None,
        "server_now": S["now"],
        "company": "Ampera",
        "role_title": "Backend Engineer",
        "jd_text": "Build APIs",
        "full_name": "Asha Rao",
        "skills": ["Python"],
        "turns": len(S["transcript"]),
        **kw,
    }


def hr_row(**kw):
    return {
        "id": IID,
        "round_id": RID,
        "drive_id": DRIVE,
        "company": "Ampera",
        "role_title": "Backend Engineer",
        "candidate_id": CID,
        "student_id": "S1",
        "full_name": "Asha Rao",
        "status": S["status"],
        "started_at": S.get("started_at"),
        "ended_at": None,
        "turns": len(S["transcript"]),
        "ai_scores": S.get("ai_scores"),
        "has_notes": False,
        "transcript": S["transcript"],
        "hr_notes": None,
        "skills": ["Python"],
        "deadline_at": None,
        "has_transcript": bool(S["transcript"]),
        **kw,
    }


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    S.clear()
    CALLS.clear()
    frames.clear()
    S.update(status="scheduled", transcript=[], now=NOW, owner_ok=True)
    monkeypatch.setattr(get_settings(), "gemini_api_key", "k")

    async def find_for_sign_in(_, sid, rid):
        if sid.lower() != "s1":
            return []
        return [
            {"candidate_id": CID, "dob": DOB, "full_name": "Asha Rao", "round_id": RID,
             "minutes": 40, "company": "Ampera", "role_title": "Backend Engineer"}
        ]  # fmt: skip

    async def ensure_interview(*_):
        return IID

    async def context(_, i, c):
        return ctx() if (i, c) == (IID, CID) else None

    async def mark_live(*_):
        if S["status"] in ("scheduled", "live"):
            S["status"] = "live"
            S.setdefault("started_at", S["now"])

    async def transcript(*_):
        return S["transcript"]

    async def append_turns(_, i, turns, mx):
        if S["status"] != "live" or len(S["transcript"]) + len(turns) > mx:
            return None
        S["transcript"] += turns
        return len(S["transcript"])

    async def complete(*_):
        if S["status"] != "live":
            return False
        S["status"] = "completed"
        return True

    async def overrides(*_):
        return {}

    async def recent_failures(*_):
        return S.get("failures", 0)

    async def log_step(_, c, r, step, ok, meta):
        CALLS.setdefault("logs", []).append((step, ok))

    async def mint_token(**kw):
        CALLS["mint"] = kw
        return "auth_tokens/eph", "gemini-3.8-live"

    async def list_for_hr(_, owner, r, d):
        CALLS["owner"] = owner
        return [hr_row()]

    async def get_for_hr(_, i, owner):
        CALLS["owner"] = owner
        return hr_row() if S["owner_ok"] and i == IID else None

    async def owned(_, i, owner):
        CALLS["owner"] = owner
        return hr_row() if S["owner_ok"] and i == IID else None

    async def set_notes(_, i, n):
        CALLS["notes"] = n

    async def mark_pending(*_):
        S["ai_scores"] = {"pending": True}

    async def audit(_, actor, action, entity, eid, diff=None):
        CALLS.setdefault("audit", []).append(action)

    async def run_scoring(iid, client=None):
        CALLS.setdefault("scored", []).append(iid)

    for mod, name, fn in [
        (repo, "find_for_sign_in", find_for_sign_in),
        (repo, "ensure_interview", ensure_interview),
        (repo, "context", context),
        (repo, "mark_live", mark_live),
        (repo, "transcript", transcript),
        (repo, "append_turns", append_turns),
        (repo, "complete", complete),
        (repo, "prompt_overrides", overrides),
        (repo, "list_for_hr", list_for_hr),
        (repo, "get_for_hr", get_for_hr),
        (repo, "owned", owned),
        (repo, "set_notes", set_notes),
        (repo, "mark_pending", mark_pending),
        (auth_repo, "recent_failures", recent_failures),
        (auth_repo, "log_step", log_step),
        (gemini, "mint_token", mint_token),
        (service, "audit", audit),
        (scoring, "run_scoring", run_scoring),
    ]:
        monkeypatch.setattr(mod, name, fn)
    assert audit_mod  # imported for clarity: service binds `audit` at import time
    app.dependency_overrides[get_session] = lambda: FakeSession()
    yield
    app.dependency_overrides.clear()


client = TestClient(app)


def student():
    return {"Authorization": "Bearer " + tokens.issue(IID, CID)}


def as_staff(user=HR):
    app.dependency_overrides[current_user] = lambda: user


# ------------------------------------------------------------------------------- sign-in


def test_start_issues_interview_token_without_any_pipeline_step():
    r = client.post("/api/v1/interview/start", json={"student_id": "S1", "dob": "2004-05-01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "scheduled" and body["duration_min"] == 40
    assert body["seconds_left"] is None  # the clock hasn't started
    assert tokens.decode(body["interview_token"]).interview_id == IID
    assert CALLS["logs"] == [("id_dob", True)]


@pytest.mark.parametrize(
    "payload",
    [{"student_id": "S1", "dob": "2004-05-02"}, {"student_id": "nobody", "dob": "2004-05-01"}],
)
def test_start_gives_one_answer_for_wrong_dob_and_unknown_id(payload):
    r = client.post("/api/v1/interview/start", json=payload)
    assert r.status_code == 401 and r.json()["error"]["code"] == "ENTRY_MISMATCH"


def test_start_locks_after_too_many_failures():
    S["failures"] = 5
    r = client.post("/api/v1/interview/start", json={"student_id": "S1", "dob": "2004-05-01"})
    assert r.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"


def test_other_token_types_are_refused():
    entry = jwt.encode(
        {"typ": "entry", "cid": str(CID), "exp": time.time() + 600},
        get_settings().entry_signing_key,
        algorithm="HS256",
    )
    r = client.post("/api/v1/interview/session", headers={"Authorization": f"Bearer {entry}"})
    assert r.status_code == 401
    assert client.post("/api/v1/interview/session").status_code == 401


# ------------------------------------------------------------------------------- session


def test_session_starts_clock_and_mints_locked_token():
    r = client.post("/api/v1/interview/session", headers=student())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ephemeral_token"] == "auth_tokens/eph" and body["model"] == "gemini-3.8-live"
    assert body["seconds_left"] == 40 * 60 and body["resumed"] is False
    assert S["status"] == "live"
    instruction = CALLS["mint"]["system_instruction"]
    assert "Backend Engineer" in instruction and "Asha Rao" in instruction
    assert instruction not in r.text and "systemInstruction" not in body["client_config"]
    assert body["client_config"]["tools"][0]["functionDeclarations"][0]["name"] == "show_answer_box"
    assert body["client_config"]["sessionResumption"] == {}
    assert body["protocol"]["closing_phrase"] == "That concludes our interview."
    assert CALLS["mint"]["expire_at"] == NOW + timedelta(minutes=45)


def test_session_after_reload_resumes_with_earlier_turns():
    S.update(status="live", started_at=NOW - timedelta(minutes=30))
    S["transcript"] = [{"role": "candidate", "text": "my project", "ts": "x"}]
    body = client.post("/api/v1/interview/session", headers=student()).json()
    assert body["resumed"] is True and body["seconds_left"] == 600
    assert "Do NOT greet again" in CALLS["mint"]["system_instruction"]


@pytest.mark.parametrize(
    ("state", "code"),
    [
        ({"status": "completed"}, "INTERVIEW_COMPLETED"),
        ({"status": "live", "started_at": NOW - timedelta(minutes=41)}, "INTERVIEW_EXPIRED"),
    ],
)
def test_session_refused_when_finished(state, code):
    S.update(state)
    r = client.post("/api/v1/interview/session", headers=student())
    assert r.status_code == 409 and r.json()["error"]["code"] == code


def test_session_without_key_does_not_start_the_clock(monkeypatch):
    monkeypatch.setattr(get_settings(), "gemini_api_key", None)
    r = client.post("/api/v1/interview/session", headers=student())
    assert r.status_code == 503 and r.json()["error"]["code"] == "AI_NOT_CONFIGURED"
    assert S["status"] == "scheduled"


# ---------------------------------------------------------------------------- transcript


def _live():
    S.update(status="live", started_at=NOW - timedelta(minutes=5))


TURN = {"role": "candidate", "text": "  I built an app  ", "ts": "2026-11-13T10:05:00Z"}


def test_transcript_appends_turns():
    _live()
    r = client.post("/api/v1/interview/transcript", headers=student(), json={"turns": [TURN]})
    assert r.json() == {"saved": 1, "status": "live"}
    assert S["transcript"][0]["text"] == "I built an app"


@pytest.mark.parametrize(
    "body",
    [
        {"turns": [TURN] * 51},
        {"turns": [{**TURN, "text": "x" * 2001}]},
        {"turns": [{**TURN, "text": "   "}]},
        {"turns": [{**TURN, "role": "system"}]},
    ],
)
def test_transcript_limits(body):
    _live()
    r = client.post("/api/v1/interview/transcript", headers=student(), json=body)
    assert r.status_code == 422


def test_transcript_total_cap():
    _live()
    S["transcript"] = [TURN] * 1500
    r = client.post("/api/v1/interview/transcript", headers=student(), json={"turns": [TURN]})
    assert r.json()["error"]["code"] == "TRANSCRIPT_TOO_LONG"


def test_ended_completes_and_queues_scoring_once():
    _live()
    r = client.post(
        "/api/v1/interview/transcript", headers=student(), json={"turns": [TURN], "ended": True}
    )
    assert r.json() == {"saved": 1, "status": "completed"}
    assert CALLS["scored"] == [IID]
    again = client.post("/api/v1/interview/transcript", headers=student(), json={"ended": True})
    assert again.json()["status"] == "completed" and CALLS["scored"] == [IID]


def test_turns_after_the_grace_period_are_refused():
    S.update(status="live", started_at=NOW - timedelta(minutes=43))
    r = client.post("/api/v1/interview/transcript", headers=student(), json={"turns": [TURN]})
    assert r.json()["error"]["code"] == "INTERVIEW_EXPIRED"


# -------------------------------------------------------------------------------- frames


def _post_frame(data, **headers):
    return client.post(
        "/api/v1/interview/frame",
        headers={**student(), "Content-Type": "image/jpeg", **headers},
        content=data,
    )


def test_frame_kept_in_memory_and_served_to_owning_hr():
    _live()
    assert _post_frame(JPEG).status_code == 204
    as_staff()
    r = client.get(f"/api/v1/interviews/{IID}/frame")
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "no-store"
    assert CALLS["owner"] == HR.id


def test_frame_checks():
    _live()
    assert _post_frame(b"\x89PNG" + b"0" * 100).json()["error"]["code"] == "FRAME_NOT_JPEG"
    assert _post_frame(JPEG + b"0" * frames.MAX_BYTES).status_code == 413
    S["status"] = "completed"
    assert _post_frame(JPEG).json()["error"]["code"] == "INTERVIEW_NOT_LIVE"


def test_stale_or_unowned_frame_is_404():
    _live()
    as_staff()
    assert client.get(f"/api/v1/interviews/{IID}/frame").json()["error"]["code"] == "NO_FRAME"
    _post_frame(JPEG)
    S["owner_ok"] = False
    assert client.get(f"/api/v1/interviews/{IID}/frame").status_code == 404


# ------------------------------------------------------------------------------------ HR


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/interviews"),
        ("get", f"/api/v1/interviews/{IID}"),
        ("get", f"/api/v1/interviews/{IID}/frame"),
        ("post", f"/api/v1/interviews/{IID}/evaluate"),
        ("patch", f"/api/v1/interviews/{IID}"),
    ],
)
def test_hr_routes_need_staff_and_refuse_student_tokens(method, path):
    r = getattr(client, method)(
        path, headers=student(), **({"json": {}} if method != "get" else {})
    )
    assert r.status_code == 401


def test_list_scopes_hr_to_their_drives_and_admin_to_all():
    S["ai_scores"] = {"communication": 4, "technical_depth": 3, "problem_solving": 3,
                      "culture": 4}  # fmt: skip
    as_staff()
    row = client.get("/api/v1/interviews").json()[0]
    assert CALLS["owner"] == HR.id
    assert row["scoring"] == "done" and row["average"] == 3.5
    as_staff(ADMIN)
    client.get("/api/v1/interviews")
    assert CALLS["owner"] is None


@pytest.mark.parametrize(
    ("scores", "state"), [({"pending": True}, "pending"), ({"error": "X"}, "error"), (None, None)]
)
def test_scoring_state(scores, state):
    S["ai_scores"] = scores
    as_staff()
    assert client.get(f"/api/v1/interviews/{IID}").json()["scoring"] == state


def test_detail_404_when_not_owned():
    S["owner_ok"] = False
    as_staff()
    r = client.get(f"/api/v1/interviews/{IID}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "INTERVIEW_NOT_FOUND"


def test_evaluate_needs_a_transcript_then_queues_and_audits():
    as_staff()
    r = client.post(f"/api/v1/interviews/{IID}/evaluate")
    assert r.json()["error"]["code"] == "NO_TRANSCRIPT"
    S["transcript"] = [TURN]
    r = client.post(f"/api/v1/interviews/{IID}/evaluate")
    assert r.status_code == 202 and r.json() == {"scoring": "pending"}
    assert CALLS["scored"] == [IID] and CALLS["audit"] == ["interview.evaluate"]


def test_notes_saved_and_audited():
    as_staff()
    r = client.patch(f"/api/v1/interviews/{IID}", json={"hr_notes": "Strong on SQL"})
    assert r.status_code == 200 and CALLS["notes"] == "Strong on SQL"
    assert CALLS["audit"] == ["interview.notes"]
    too_long = client.patch(f"/api/v1/interviews/{IID}", json={"hr_notes": "x" * 5001})
    assert too_long.status_code == 422


def test_typed_answer_keeps_code_indentation_and_language():
    _live()
    code = "def f(x):\n    return x[::-1]\n"
    typed = {"role": "candidate", "kind": "typed", "language": "python", "text": code,
             "ts": "2026-11-13T10:06:00Z"}  # fmt: skip
    r = client.post("/api/v1/interview/transcript", headers=student(), json={"turns": [typed]})
    assert r.status_code == 200, r.text
    saved = S["transcript"][0]
    assert saved["text"] == "def f(x):\n    return x[::-1]" and saved["language"] == "python"


@pytest.mark.parametrize(
    "turn",
    [
        {"role": "ai", "kind": "typed", "text": "x"},  # only the candidate types
        {"role": "candidate", "kind": "question", "text": "x"},  # only the AI asks
        {"role": "candidate", "kind": "typed", "text": "x" * 8001},
        {"role": "candidate", "kind": "spoken", "text": "x" * 2001},
    ],
)
def test_typed_turn_rules(turn):
    _live()
    body = {"turns": [{**turn, "ts": "2026-11-13T10:06:00Z"}]}
    assert (
        client.post("/api/v1/interview/transcript", headers=student(), json=body).status_code == 422
    )


def test_ping_is_public_and_uncached():
    r = client.get("/api/v1/interview/ping")
    assert r.status_code == 204 and r.headers["cache-control"] == "no-store"

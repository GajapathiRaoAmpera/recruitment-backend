"""PUT /attempt/code-drafts/{qid} and GET /attempt/code-drafts (FR-17, AC-6). DB is faked;
tests/execution/test_end_to_end.py runs the SQL on a real Postgres."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.execution import repo, service
from app.execution.deps import AttemptContext, student_attempt
from app.main import app

CTX = AttemptContext(attempt_id=uuid.uuid4(), candidate_id=uuid.uuid4())
QID = uuid.uuid4()
BODY = {"code": "print(input())", "language": "python"}
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def env(monkeypatch):
    state = dict(
        attempt={"status": "in_progress", "past_deadline": False},
        in_test=True,
        saved=[],
        drafts=[],
        session=FakeSession(),
    )

    async def get_attempt(_, attempt_id):
        return state["attempt"]

    async def in_test(_, attempt_id, question_id):
        return state["in_test"]

    async def save_draft(_, *args):
        state["saved"].append(args)

    async def newer_drafts(_, attempt_id):
        assert attempt_id == CTX.attempt_id
        return state["drafts"]

    monkeypatch.setattr(repo, "get_attempt", get_attempt)
    monkeypatch.setattr(repo, "is_coding_question_of_attempt", in_test)
    monkeypatch.setattr(repo, "save_draft", save_draft)
    monkeypatch.setattr(repo, "newer_drafts", newer_drafts)
    app.dependency_overrides[get_session] = lambda: state["session"]
    app.dependency_overrides[student_attempt] = lambda: CTX
    yield state
    app.dependency_overrides.clear()
    service._in_flight.clear()


def put(body=BODY, qid=QID):
    return TestClient(app).put(f"/api/v1/attempt/code-drafts/{qid}", json=body)


def test_both_routes_need_a_student_attempt_token():
    c = TestClient(app)
    for r in (
        c.put(f"/api/v1/attempt/code-drafts/{QID}", json=BODY),
        c.get("/api/v1/attempt/code-drafts"),
    ):
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_put_saves_the_draft(env):
    r = put()
    assert r.status_code == 204 and r.content == b""
    assert env["saved"] == [(CTX.attempt_id, QID, BODY["code"], "python")]
    assert env["session"].commits == 1


def test_an_empty_editor_is_a_valid_draft(env):
    assert put({"code": "", "language": "java"}).status_code == 204
    assert env["saved"][0][2:] == ("", "java")


def test_a_draft_does_not_wait_for_a_running_judge0_call(env):
    service._in_flight.add(CTX.candidate_id)  # a Run of theirs is still going
    assert put().status_code == 204


@pytest.mark.parametrize(
    "patch",
    [{"language": "cobol"}, {"code": "x" * 64_001}, {"extra": 1}, {"code": None}],
)
def test_bad_body_is_422(env, patch):
    r = put({**BODY, **patch})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert env["saved"] == []


def test_missing_fields_are_422(env):
    assert put({"code": "x"}).status_code == 422
    assert put({"language": "python"}).status_code == 422


@pytest.mark.parametrize(
    "attempt,code",
    [
        ({"status": "locked", "past_deadline": False}, "ATTEMPT_LOCKED"),
        ({"status": "submitted", "past_deadline": False}, "ATTEMPT_LOCKED"),
        ({"status": "not_started", "past_deadline": False}, "ATTEMPT_LOCKED"),
        ({"status": "expired", "past_deadline": True}, "ATTEMPT_EXPIRED"),
        ({"status": "in_progress", "past_deadline": True}, "ATTEMPT_EXPIRED"),
        ({"status": "submitted", "past_deadline": True, "auto_submitted": True}, "ATTEMPT_EXPIRED"),
    ],
)
def test_put_needs_an_open_attempt(env, attempt, code):
    env["attempt"] = attempt
    r = put()
    assert r.status_code == 409 and r.json()["error"]["code"] == code
    assert env["saved"] == []


def test_put_for_a_question_outside_this_test_is_404(env):
    env["in_test"] = False  # also an MCQ id: drafts are for coding questions only
    r = put()
    assert r.status_code == 404 and r.json()["error"]["code"] == "QUESTION_NOT_FOUND"
    assert env["saved"] == []


def test_put_with_a_bad_question_id_is_422(env):
    assert put(qid="nope").status_code == 422


def test_get_returns_the_newer_drafts_by_question(env):
    env["drafts"] = [
        {
            "question_id": QID,
            "draft_code": "x = 1",
            "draft_language": "python",
            "draft_saved_at": NOW,
        }
    ]
    r = TestClient(app).get("/api/v1/attempt/code-drafts")
    assert r.status_code == 200
    assert r.json() == {
        "drafts": {
            str(QID): {"code": "x = 1", "language": "python", "saved_at": "2026-11-13T10:00:00Z"}
        }
    }


def test_get_works_after_the_attempt_is_closed(env):
    env["attempt"] = {"status": "submitted", "past_deadline": True}
    r = TestClient(app).get("/api/v1/attempt/code-drafts")
    assert r.status_code == 200 and r.json() == {"drafts": {}}


# --- the SQL ---------------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.sql: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        self.sql.append((str(stmt), params or {}))

        class Rows:
            def mappings(self):
                return []

        return Rows()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_save_draft_touches_only_the_draft_columns():
    s = Recorder()
    await repo.save_draft(s, CTX.attempt_id, QID, "x", "python")
    ((sql, params),) = s.sql
    update = sql.split("do update set", 1)[1]
    assert "draft_code" in update and "draft_saved_at" in update
    for col in ("code =", "language =", "passed_cases", "total_cases", "marks", "updated_at"):
        assert f" {col}" not in update.replace("draft_", "DRAFT_")
    assert params == {"a": CTX.attempt_id, "q": QID, "code": "x", "lang": "python"}


@pytest.mark.anyio
async def test_newer_drafts_compares_with_the_last_submit():
    s = Recorder()
    assert await repo.newer_drafts(s, CTX.attempt_id) == []
    sql = s.sql[0][0]
    assert "passed_cases is null or draft_saved_at >= updated_at" in sql

"""POST /attempt/run and /attempt/submit-code (FR-16, AC-5). DB and Judge0 are fakes."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.execution import repo, service
from app.execution.deps import AttemptContext, judge0_client, student_attempt
from app.execution.judge0 import RunResult
from app.execution.scoring import CaseSpec
from app.main import app

CTX = AttemptContext(attempt_id=uuid.uuid4(), candidate_id=uuid.uuid4())
QID = uuid.uuid4()
BODY = {"question_id": str(QID), "code": "print(input())", "language": "python"}
SECRET = "HIDDEN-EXPECTED-VALUE"


def ran(stdout="", **kw) -> RunResult:
    base = dict(stderr="", compile_output="", message="", time_s=0.01, memory_kb=3000, exit_code=0)
    return RunResult(status_id=3, status="Accepted", stdout=stdout, **{**base, **kw})


class FakeSession:
    commits = 0

    async def commit(self):
        self.commits += 1


class FakeJudge0:
    """Echoes stdin. Records every call so tests can see what was sent to Judge0."""

    def __init__(self):
        self.calls = []
        self.fail = None

    async def run_many(self, language, source, stdins):
        self.calls.append(list(stdins))
        if self.fail:
            raise self.fail
        return [ran(s) for s in stdins]


@pytest.fixture
def env(monkeypatch):
    cases = [CaseSpec(f"s{i}", f"s{i}", True) for i in range(3)] + [
        CaseSpec(f"h{i}", f"h{i}", False) for i in range(15)
    ]
    state = dict(
        attempt={"status": "in_progress", "past_deadline": False},
        in_test=True,
        cases=cases,
        saved=[],
        loads=[],
        session=FakeSession(),
        judge0=FakeJudge0(),
    )

    async def get_attempt(_, attempt_id):
        return state["attempt"]

    async def in_test(_, attempt_id, question_id):
        return state["in_test"]

    async def load_cases(_, question_id, *, samples_only):
        state["loads"].append(samples_only)
        return [c for c in state["cases"] if c.is_sample or not samples_only]

    async def save(_, *args):
        state["saved"].append(args)

    monkeypatch.setattr(repo, "get_attempt", get_attempt)
    monkeypatch.setattr(repo, "is_coding_question_of_attempt", in_test)
    monkeypatch.setattr(repo, "load_cases", load_cases)
    monkeypatch.setattr(repo, "save_submission", save)
    app.dependency_overrides[get_session] = lambda: state["session"]
    app.dependency_overrides[judge0_client] = lambda: state["judge0"]
    app.dependency_overrides[student_attempt] = lambda: CTX
    service._in_flight.clear()
    yield state
    app.dependency_overrides.clear()
    service._in_flight.clear()


def post(path, body=BODY):
    return TestClient(app).post(f"/api/v1/attempt/{path}", json=body)


@pytest.mark.parametrize("path", ["run", "submit-code"])
def test_needs_a_student_attempt_token(path):
    # no override: until the attempt token exists (1.3) the routes refuse everyone
    r = post(path)
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"


# --- Run -------------------------------------------------------------------------------------


def test_run_returns_the_three_sample_results_and_never_loads_hidden(env):
    r = post("run")
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["case"] for x in results] == [1, 2, 3]
    assert all(x["passed"] and x["status"] == "passed" for x in results)
    assert set(results[0]) == {"case", "passed", "status", "stdout", "stderr", "time"}
    assert env["loads"] == [True]  # samples_only: hidden cases were never read
    assert env["judge0"].calls == [["s0", "s1", "s2"]]
    assert env["saved"] == []  # Run saves nothing


def test_run_reports_a_failing_sample(env):
    env["cases"][1] = CaseSpec("s1", "something else", True)
    results = post("run").json()["results"]
    assert [x["status"] for x in results] == ["passed", "wrong_answer", "passed"]
    assert [x["passed"] for x in results] == [True, False, True]


# --- Submit ----------------------------------------------------------------------------------


def test_submit_returns_counts_only_and_saves_them(env):
    env["cases"][5] = CaseSpec("h2", SECRET, False)  # one hidden case fails
    r = post("submit-code")
    assert r.status_code == 200
    assert r.json() == {"passed": 17, "total": 18}
    assert SECRET not in r.text and "h2" not in r.text
    assert env["loads"] == [False]
    assert len(env["judge0"].calls[0]) == 18
    assert env["saved"] == [(CTX.attempt_id, QID, BODY["code"], "python", 17, 18)]
    assert env["session"].commits >= 1


def test_submit_does_not_save_when_judge0_is_down(env):
    from app.core.errors import AppError

    env["judge0"].fail = AppError(503, "EXECUTION_UNAVAILABLE", "Code execution is unavailable.")
    r = post("submit-code")
    assert r.status_code == 503 and r.json()["error"]["code"] == "EXECUTION_UNAVAILABLE"
    assert env["saved"] == []


def test_submit_with_no_cases_is_a_409(env):
    env["cases"] = []
    r = post("submit-code")
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_TEST_CASES"
    assert env["saved"] == []


# --- Guards (both routes) --------------------------------------------------------------------


@pytest.mark.parametrize("path", ["run", "submit-code"])
@pytest.mark.parametrize(
    "attempt,code",
    [
        ({"status": "locked", "past_deadline": False}, "ATTEMPT_LOCKED"),
        ({"status": "submitted", "past_deadline": True}, "ATTEMPT_LOCKED"),
        ({"status": "not_started", "past_deadline": False}, "ATTEMPT_LOCKED"),
        ({"status": "expired", "past_deadline": True}, "ATTEMPT_EXPIRED"),
        ({"status": "in_progress", "past_deadline": True}, "ATTEMPT_EXPIRED"),
    ],
)
def test_attempt_must_be_open(env, path, attempt, code):
    env["attempt"] = attempt
    r = post(path)
    assert r.status_code == 409 and r.json()["error"]["code"] == code
    assert env["judge0"].calls == [] and env["saved"] == []


@pytest.mark.parametrize("path", ["run", "submit-code"])
def test_missing_attempt_is_404(env, path):
    env["attempt"] = None
    assert post(path).json()["error"]["code"] == "ATTEMPT_NOT_FOUND"


@pytest.mark.parametrize("path", ["run", "submit-code"])
def test_question_must_be_a_coding_question_of_this_test(env, path):
    env["in_test"] = False
    r = post(path)
    assert r.status_code == 404 and r.json()["error"]["code"] == "QUESTION_NOT_FOUND"
    assert env["loads"] == [] and env["judge0"].calls == []


@pytest.mark.parametrize("path", ["run", "submit-code"])
def test_one_run_in_flight_per_candidate(env, path):
    service._in_flight.add(CTX.candidate_id)  # a run of theirs is still going
    r = post(path)
    assert r.status_code == 429 and r.json()["error"]["code"] == "RUN_IN_PROGRESS"
    assert env["judge0"].calls == []


@pytest.mark.parametrize("path", ["run", "submit-code"])
def test_in_flight_flag_is_released_after_success_and_failure(env, path):
    assert post(path).status_code == 200
    assert CTX.candidate_id not in service._in_flight
    env["judge0"].fail = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        TestClient(app, raise_server_exceptions=True).post(f"/api/v1/attempt/{path}", json=BODY)
    assert CTX.candidate_id not in service._in_flight


@pytest.mark.parametrize("path", ["run", "submit-code"])
@pytest.mark.parametrize(
    "patch",
    [
        {"language": "cobol"},
        {"code": ""},
        {"question_id": "not-a-uuid"},
        {"code": "x" * 64_001},
        {"extra": 1},
    ],
)
def test_bad_body_is_422(env, path, patch):
    r = post(path, {**BODY, **patch})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert env["judge0"].calls == []

"""POST /questions/generate: AI drafts of coding questions. The LLM, Judge0 and the DB are all
faked here; nothing leaves the process. The live run is in test_generate_coding_live.py."""

import asyncio
import inspect
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.attempts import state_repo as attempt_repo
from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.core.errors import AppError
from app.execution import repo as execution_repo
from app.execution.coding_questions import generate as gen
from app.execution.coding_questions import repo, service
from app.execution.deps import judge0_client
from app.execution.judge0 import ACCEPTED, RunResult
from app.main import app
from app.papers.agents import LlmOutputError, parse_json_object

HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
NOW = datetime.now(UTC)
SECRET_REF = "print('TOP-SECRET-REFERENCE')"
GOOD_SOLUTION = "import sys\nprint(sys.stdin.read().strip())\n"
URL = "/api/v1/questions/generate"


def reply(title="Pair Sums", solution=GOOD_SOLUTION, **over) -> dict:
    return {
        "title": title,
        "statement": "Count pairs.\nInput format: n then n ints.\nOutput format: one int.",
        "reference_solution": solution,
        "sample_inputs": [f"1\n{i}" for i in range(3)],
        "hidden_inputs": [f"2\n{i} {i}" for i in range(15)],
        **over,
    }


def ok_run(stdout: str, time_s: float = 0.05) -> RunResult:
    return RunResult(ACCEPTED, "Accepted", stdout, "", "", "", time_s, 9000, 0)


class FakeLlm:
    """Replies scripted per question index (read from the prompt). A reply is a dict, an
    exception, or a raw string that goes through the real JSON parser."""

    model = "fake-model"

    def __init__(self, script: dict[int, list]):
        self.script = {i: list(r) for i, r in script.items()}
        self.prompts: dict[int, list[str]] = {}

    async def complete_json(self, *, system: str, user: str) -> dict:
        m = re.search(r"problem (\d+) of", user)
        i = int(m.group(1)) - 1 if m else 0
        self.prompts.setdefault(i, []).append(user)
        r = self.script[i].pop(0)
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, str):
            return parse_json_object(r)
        return r


class FakeJudge0:
    """Echo program: stdout is the stripped stdin. Sources containing CRASH fail, SLOW is slow,
    SAME prints a constant."""

    def __init__(self, error: AppError | None = None, times: int | None = None):
        self.calls: list[tuple[str, str, list[str]]] = []
        self.error, self.times = error, times  # raise `error` `times` times (None = always)

    async def run_many(self, language, source, stdins):
        self.calls.append((language, source, stdins))
        if self.error is not None and (self.times is None or self.times > 0):
            if self.times is not None:
                self.times -= 1
            raise self.error
        if "CRASH" in source:
            return [
                RunResult(11, "Runtime Error (NZEC)", "", "ZeroDivisionError", "", "", 0.01, 1, 1)
                for _ in stdins
            ]
        if "SAME" in source:
            return [ok_run("42\n") for _ in stdins]
        if "SLOW" in source:
            return [ok_run("x\n", time_s=4.0) for _ in stdins]
        return [ok_run(f"out:{s.strip()}\n") for s in stdins]


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def db(monkeypatch):
    questions, case_rows, audits = {}, {}, []
    state = {"titles": []}
    session = FakeSession()

    async def insert_question(_, owner, body_, difficulty, tags, marks, **ref):
        ai = ref.get("reference_solution") is not None
        row = dict(
            id=uuid.uuid4(),
            body=body_,
            difficulty=difficulty,
            tags=tags,
            marks=marks,
            approved=not ai,
            created_by=owner,
            created_at=NOW,
            reference_solution=ref.get("reference_solution"),
            reference_language=ref.get("reference_language"),
        )
        questions[row["id"]] = row
        return row

    async def insert_cases(_, qid, cs):
        case_rows[qid] = [
            dict(id=uuid.uuid4(), position=i, **c.model_dump()) for i, c in enumerate(cs)
        ]

    async def get_question(_, qid, owner):
        q = questions.get(qid)
        return q if q and (owner is None or q["created_by"] == owner) else None

    async def get_cases(_, qid):
        return case_rows.get(qid, [])

    async def recent_titles(_, owner, limit):
        return state["titles"][:limit]

    async def audit(_, actor_id, action, entity, entity_id, diff=None):
        audits.append((actor_id, action, entity_id, diff))

    for name, fn in dict(
        insert_question=insert_question,
        insert_cases=insert_cases,
        get_question=get_question,
        get_cases=get_cases,
        recent_titles=recent_titles,
    ).items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(service, "audit", audit)
    app.dependency_overrides[get_session] = lambda: session
    yield SimpleNamespace(
        questions=questions, cases=case_rows, audits=audits, session=session, state=state
    )
    app.dependency_overrides.clear()


def use(llm: FakeLlm | None = None, judge0: FakeJudge0 | None = None):
    app.dependency_overrides[security.current_user] = lambda: HR
    if llm is not None:
        app.dependency_overrides[gen.generation_llm] = lambda: llm
    j = judge0 or FakeJudge0()
    app.dependency_overrides[judge0_client] = lambda: j
    return TestClient(app), j


def post(client, **body):
    return client.post(URL, json={"difficulty": "medium", **body})


# --- happy path ------------------------------------------------------------------------------


def test_happy_path_saves_18_cases_with_expected_from_judge0_unapproved_and_audited(db):
    llm = FakeLlm({0: [reply()]})
    c, j = use(llm)
    r = post(c, topic="arrays", tags=["arrays"], marks=5)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["failed"] == [] and len(out["created"]) == 1
    q = out["created"][0]
    assert q["approved"] is False and q["kind"] == "coding" and q["difficulty"] == "medium"
    assert q["body"].startswith("Pair Sums\nCount pairs.")
    assert q["tags"] == ["arrays"] and float(q["marks"]) == 5
    assert q["reference_solution"] == GOOD_SOLUTION.strip("\n")
    assert q["reference_language"] == "python"
    # 3 sample then 15 hidden; every expected is Judge0's stdout for that input
    cases = q["test_cases"]
    assert [x["is_sample"] for x in cases] == [True] * 3 + [False] * 15
    assert all(x["expected"] == f"out:{x['input'].strip()}\n" for x in cases)
    assert all(x["input"].endswith("\n") for x in cases)
    # one Judge0 batch, Python, all 18 inputs
    assert len(j.calls) == 1 and j.calls[0][0] == "python" and len(j.calls[0][2]) == 18
    # DB connection released before the slow part; one commit for the question + its audit
    assert db.session.rollbacks == 1 and db.session.commits == 1
    (actor, action, entity_id, diff) = db.audits[0]
    assert (actor, action, entity_id) == (HR.id, "question.generate", uuid.UUID(q["id"]))
    assert diff == {
        "kind": "coding",
        "difficulty": "medium",
        "test_cases": 18,
        "reference_language": "python",
        "model": "fake-model",
    }
    assert "print" not in str(db.audits) and "out:" not in str(db.audits)  # counts only
    assert "Topic: arrays" in llm.prompts[0][0]


def test_hr_sees_the_reference_solution_on_get_and_manual_questions_have_null(db):
    c, _ = use(FakeLlm({0: [reply()]}))
    qid = post(c).json()["created"][0]["id"]
    got = c.get(f"/api/v1/questions/{qid}").json()
    assert got["reference_solution"] and got["reference_language"] == "python"
    manual = c.post(
        "/api/v1/questions",
        json={
            "kind": "coding",
            "body": "Manual",
            "difficulty": "easy",
            "test_cases": [{"input": "", "expected": "1", "is_sample": i < 3} for i in range(18)],
        },
    ).json()
    assert manual["approved"] is True
    assert manual["reference_solution"] is None and manual["reference_language"] is None


def test_avoid_list_reaches_the_prompt_and_a_repeated_title_is_a_failure(db):
    db.state["titles"] = ["Pair Sums", "Old Problem"]
    llm = FakeLlm({0: [reply(title="pair  sums!")]})
    c, _ = use(llm)
    r = post(c)
    assert r.status_code == 502 and r.json()["error"]["code"] == "AI_BAD_OUTPUT"
    assert "duplicate title" in r.json()["error"]["details"]["failed"][0]["reason"]
    assert "- Pair Sums\n- Old Problem" in llm.prompts[0][0]


# --- retries ---------------------------------------------------------------------------------


def test_bad_json_is_retried_once_with_the_error_then_fails(db):
    llm = FakeLlm({0: ['{"title": "x", "hidden_inputs": ["a" + "b"]}', "not json at all"]})
    c, j = use(llm)
    r = post(c)
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["code"] == "AI_BAD_OUTPUT"
    assert err["details"]["failed"] == [{"index": 0, "reason": "model reply was not valid JSON"}]
    assert len(llm.prompts[0]) == 2 and j.calls == []
    retry = llm.prompts[0][1]
    assert "could not be used" in retry and "JSON error: Expecting" in retry and '"a" +' in retry
    assert db.questions == {} and db.session.commits == 0


def test_bad_json_then_a_good_reply_succeeds(db):
    c, _ = use(FakeLlm({0: [LlmOutputError("bad"), reply()]}))
    r = post(c)
    assert r.status_code == 201 and len(r.json()["created"]) == 1


def test_schema_invalid_reply_is_retried_with_the_validation_errors(db):
    llm = FakeLlm({0: [reply(hidden_inputs=["1"] * 3), reply(sample_inputs=["", "a", "b"])]})
    c, _ = use(llm)
    r = post(c)
    assert r.status_code == 502
    assert "failed validation" in r.json()["error"]["details"]["failed"][0]["reason"]
    assert "hidden_inputs" in llm.prompts[0][1]


def test_crashing_reference_solution_is_retried_with_stderr_then_fails(db):
    llm = FakeLlm({0: [reply(solution="CRASH 1"), reply(solution="CRASH 2")]})
    c, j = use(llm)
    r = post(c)
    assert r.status_code == 502
    reason = r.json()["error"]["details"]["failed"][0]["reason"]
    assert reason == "reference solution failed on sample input 1: Runtime Error (NZEC)"
    assert len(j.calls) == 2
    assert "ZeroDivisionError" in llm.prompts[0][1] and "Input:\n1\n0" in llm.prompts[0][1]
    assert db.questions == {}


def test_crash_then_a_working_solution_succeeds(db):
    c, j = use(FakeLlm({0: [reply(solution="CRASH"), reply()]}))
    r = post(c)
    assert r.status_code == 201 and len(j.calls) == 2


def test_judge0_timeout_counts_as_a_failed_try(db):
    timeout = AppError(504, "EXECUTION_TIMEOUT", "slow")
    j = FakeJudge0(error=timeout, times=1)
    c, _ = use(FakeLlm({0: [reply(), reply()]}), j)
    r = post(c)
    assert r.status_code == 201 and len(j.calls) == 2


# --- several questions -----------------------------------------------------------------------


def test_partial_success_two_of_three(db):
    llm = FakeLlm(
        {
            0: [reply(title="A")],
            1: [reply(title="B", solution="CRASH"), reply(title="B", solution="CRASH")],
            2: [reply(title="C")],
        }
    )
    c, _ = use(llm)
    r = post(c, count=3)
    assert r.status_code == 201
    out = r.json()
    assert [q["body"].split("\n")[0] for q in out["created"]] == ["A", "C"]
    assert [f["index"] for f in out["failed"]] == [1]
    assert db.session.commits == 2 and len(db.audits) == 2
    assert all("problem" in llm.prompts[i][0] for i in range(3))


def test_two_parallel_questions_with_the_same_title_keep_one(db):
    c, _ = use(FakeLlm({0: [reply(title="Same")], 1: [reply(title="Same")]}))
    out = post(c, count=2).json()
    assert len(out["created"]) == 1 and "duplicate title" in out["failed"][0]["reason"]


# --- errors ----------------------------------------------------------------------------------


def test_missing_key_is_503_before_anything_runs(db, monkeypatch):
    monkeypatch.setattr(gen, "get_settings", lambda: SimpleNamespace(nvidia_api_key=None))
    c, j = use(llm=None)  # the real generation_llm dependency
    r = post(c)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "AI_PROVIDER_NOT_CONFIGURED"
    assert j.calls == [] and db.session.rollbacks == 0


def test_no_login_is_401_even_without_a_key(monkeypatch):
    monkeypatch.setattr(gen, "get_settings", lambda: SimpleNamespace(nvidia_api_key=None))
    r = TestClient(app).post(URL, json={"difficulty": "easy"})
    assert r.status_code == 401


def test_judge0_down_is_503_and_nothing_is_saved(db):
    down = AppError(503, "EXECUTION_UNAVAILABLE", "down")
    c, _ = use(FakeLlm({0: [reply(title="A")], 1: [reply(title="B")]}), FakeJudge0(error=down))
    r = post(c, count=2)
    assert r.status_code == 503 and r.json()["error"]["code"] == "EXECUTION_UNAVAILABLE"
    assert db.questions == {} and db.session.commits == 0


def test_provider_error_on_every_question_returns_the_provider_error(db):
    err = AppError(502, "AI_PROVIDER_ERROR", "NVIDIA could not complete the request. Try again.")
    c, _ = use(FakeLlm({0: [err], 1: [err]}))
    r = post(c, count=2)
    assert r.status_code == 502 and r.json()["error"]["code"] == "AI_PROVIDER_ERROR"


def test_provider_error_on_one_question_is_just_a_failure(db):
    err = AppError(429, "AI_PROVIDER_RATE_LIMITED", "slow down")
    c, _ = use(FakeLlm({0: [reply(title="A")], 1: [err]}))
    out = post(c, count=2).json()
    assert len(out["created"]) == 1 and out["failed"] == [{"index": 1, "reason": "slow down"}]


@pytest.mark.parametrize(
    "body",
    [
        {"count": 0},
        {"count": 4},
        {"difficulty": "hard"},
        {"topic": "x" * 201},
        {"marks": 0},
        {"tags": [f"t{i}" for i in range(11)]},
        {"unknown": 1},
    ],
)
def test_bad_request_is_422(db, body):
    c, j = use(FakeLlm({0: []}))
    r = post(c, **body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert j.calls == []


# --- time budget -----------------------------------------------------------------------------


def test_a_question_past_the_budget_fails_with_timed_out_and_others_are_kept():
    class SlowForOne(FakeLlm):
        async def complete_json(self, *, system, user):
            if "problem 2 of" in user:
                await asyncio.sleep(5)
            return await super().complete_json(system=system, user=user)

    llm = SlowForOne({0: [reply(title="Fast")], 1: [reply(title="Never")]})
    drafts, failures = asyncio.run(
        gen.generate_many(
            llm, FakeJudge0(), difficulty="easy", topic=None, count=2, avoid=[], budget_s=0.2
        )
    )
    assert [d.question.title for d in drafts] == ["Fast"]
    assert failures[0].index == 1 and failures[0].reason.startswith("timed out")


# --- schema and run checks -------------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"title": "two\nlines"},
        {"title": "   "},
        {"statement": " "},
        {"reference_solution": ""},
        {"sample_inputs": ["a", "b"]},
        {"hidden_inputs": [f"h{i}" for i in range(16)]},
        {"hidden_inputs": ["   "] + [f"h{i}" for i in range(14)]},
        {"hidden_inputs": ["x" * 2001] + [f"h{i}" for i in range(14)]},
        {"hidden_inputs": ["1\n0"] + [f"h{i}" for i in range(14)]},  # same as sample 1
        {"statement": "x" * 20_000},
    ],
)
def test_generated_question_schema_rejects(over):
    with pytest.raises(ValueError):
        gen.GeneratedCodingQuestion.model_validate(reply(**over))


def test_generated_question_ignores_extra_fields_and_adds_the_final_newline():
    q = gen.GeneratedCodingQuestion.model_validate(reply(notes="chatter"))
    assert all(s.endswith("\n") for s in q.inputs) and len(q.inputs) == 18


@pytest.mark.parametrize(
    "solution, reason",
    [
        ("SAME", "every input gives the same output"),
        ("SLOW", "reference solution is too slow on sample input 1 (4.00 s)"),
    ],
)
def test_check_runs_rejects_useless_or_slow_solutions(solution, reason):
    q = gen.GeneratedCodingQuestion.model_validate(reply(solution=solution))
    runs = asyncio.run(FakeJudge0().run_many("python", solution, q.inputs))
    assert gen.check_runs(q, runs)[0] == reason


def test_check_runs_rejects_blank_output():
    q = gen.GeneratedCodingQuestion.model_validate(reply())
    runs = [ok_run("out\n")] * 17 + [ok_run("  \n")]
    assert gen.check_runs(q, runs)[0] == "reference solution printed nothing on hidden input 15"


# --- students never get the reference solution ----------------------------------------------


def test_get_attempt_never_returns_the_reference_solution(monkeypatch):
    """Even if a student query returned the column by mistake, GET /attempt's response model
    drops it."""
    from app.auth import entry as auth_entry

    aid, cid, tid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def get_state(_, a, c):
        return {
            "id": aid,
            "test_id": tid,
            "status": "in_progress",
            "deadline_at": NOW,
            "server_now": NOW,
            "tab_switches": 0,
            "remaining_sec_on_lock": None,
            "past_deadline": False,
        }

    async def coding(*_):
        return [
            {
                "id": uuid.uuid4(),
                "body": "Pair Sums\nCount pairs.",
                "marks": 5,
                "samples": [{"input": "1\n0\n", "expected": "out\n"}],
                "reference_solution": SECRET_REF,  # simulated leak from the query
                "reference_language": "python",
            }
        ]

    async def none(*_):
        return []

    async def no_saved(*_):
        return {}

    monkeypatch.setattr(attempt_repo, "get_state", get_state)
    monkeypatch.setattr(attempt_repo, "coding_questions_for", coding)
    monkeypatch.setattr(attempt_repo, "questions_for", none)
    monkeypatch.setattr(attempt_repo, "saved_answers", no_saved)
    app.dependency_overrides[get_session] = lambda: FakeSession()
    try:
        token = auth_entry.issue_attempt_token(aid, cid)
        r = TestClient(app).get("/api/v1/attempt", headers={"Authorization": f"Bearer {token}"})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    assert "TOP-SECRET-REFERENCE" not in r.text and "reference" not in r.text


@pytest.mark.parametrize("module", [attempt_repo, execution_repo])
def test_student_queries_never_select_the_reference_columns(module):
    """GET /attempt and run/submit name their columns; keep it that way."""
    src = inspect.getsource(module)
    assert "reference_solution" not in src and "reference_language" not in src
    assert not re.search(r"select\s+(q\.)?\*", src, flags=re.IGNORECASE)

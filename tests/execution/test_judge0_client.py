"""Judge0 client (FR-15). Judge0 is mocked with httpx.MockTransport: no network, no Docker."""

import asyncio
import base64
import json

import httpx
import pytest

from app.core.errors import AppError
from app.execution.judge0 import MAX_BATCH, Judge0Client
from app.execution.languages import LANGUAGES


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def finished(token: str, stdout: str = "", status=(3, "Accepted"), **extra) -> dict:
    return {
        "token": token,
        "status": {"id": status[0], "description": status[1]},
        "stdout": b64(stdout) if stdout else None,
        "stderr": None,
        "compile_output": None,
        "message": None,
        "time": "0.012",
        "memory": 3400,
        "exit_code": 0,
        **extra,
    }


class FakeJudge0:
    """Accepts a batch, reports `pending_polls` unfinished polls, then the results."""

    def __init__(self, outputs: list[str], pending_polls: int = 0):
        self.outputs, self.pending_polls = outputs, pending_polls
        self.posted: dict | None = None
        self.post_headers: httpx.Headers | None = None
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/submissions/batch"
            assert request.url.params["base64_encoded"] == "true"
            self.posted, self.post_headers = json.loads(request.content), request.headers
            return httpx.Response(201, json=[{"token": f"t{i}"} for i in range(len(self.outputs))])
        self.polls += 1
        tokens = request.url.params["tokens"].split(",")
        if self.polls <= self.pending_polls:
            subs = [{"token": t, "status": {"id": 2, "description": "Processing"}} for t in tokens]
        else:  # Judge0 does not promise batch order: answer reversed
            subs = [finished(t, self.outputs[int(t[1:])]) for t in reversed(tokens)]
        return httpx.Response(200, json={"submissions": subs})


def client(handler, **kw) -> Judge0Client:
    return Judge0Client(
        "http://judge0.test",
        "secret-token",
        poll_interval_s=0.001,
        transport=httpx.MockTransport(handler),
        **kw,
    )


def test_run_many_sends_limits_token_and_base64_and_keeps_input_order():
    fake = FakeJudge0(["a\n", "b\n", "c\n"], pending_polls=2)
    results = asyncio.run(client(fake).run_many("python", "print(input())", ["1", "2", "3"]))

    assert [r.stdout for r in results] == ["a\n", "b\n", "c\n"]
    assert fake.polls == 3  # polled until every run left the queue
    assert fake.post_headers["x-auth-token"] == "secret-token"
    lang = LANGUAGES["python"]
    subs = fake.posted["submissions"]
    assert [base64.b64decode(s["stdin"]).decode() for s in subs] == ["1", "2", "3"]
    for s in subs:
        assert base64.b64decode(s["source_code"]).decode() == "print(input())"
        assert s["language_id"] == lang.judge0_id
        assert s["cpu_time_limit"] == lang.limits.cpu_time_s
        assert s["wall_time_limit"] == lang.limits.wall_time_s
        assert s["memory_limit"] == lang.limits.memory_kb


def test_result_fields_are_decoded():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json=[{"token": "t0"}])
        return httpx.Response(
            200,
            json={
                "submissions": [
                    finished(
                        "t0",
                        status=(6, "Compilation Error"),
                        compile_output=b64("main.c: error: ‘x’ undeclared"),
                        message=b64("boom"),
                    )
                ]
            },
        )

    r = asyncio.run(client(handler).run("c", "int main(){return x;}"))
    assert (r.status_id, r.status) == (6, "Compilation Error")
    assert "‘x’ undeclared" in r.compile_output and r.message == "boom"
    assert r.stdout == "" and r.time_s == 0.012 and r.memory_kb == 3400


def test_slow_judge0_becomes_execution_timeout():
    def always_processing(request):
        if request.method == "POST":
            return httpx.Response(201, json=[{"token": "t0"}])
        return httpx.Response(
            200, json={"submissions": [{"token": "t0", "status": {"id": 2, "description": "p"}}]}
        )

    with pytest.raises(AppError) as e:
        asyncio.run(client(always_processing, timeout_s=0.05).run("python", "pass"))
    assert (e.value.status, e.value.code) == (504, "EXECUTION_TIMEOUT")


@pytest.mark.parametrize("status", [401, 500, 503])
def test_rejected_submit_becomes_execution_unavailable(status):
    def handler(request):
        return httpx.Response(status, json={"error": "nope"})

    with pytest.raises(AppError) as e:
        asyncio.run(client(handler).run("python", "pass"))
    assert (e.value.status, e.value.code) == (503, "EXECUTION_UNAVAILABLE")
    assert "secret-token" not in str(e.value.details) + e.value.message


def test_unreachable_judge0_becomes_execution_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(AppError) as e:
        asyncio.run(client(handler).run("python", "pass"))
    assert (e.value.status, e.value.code) == (503, "EXECUTION_UNAVAILABLE")


@pytest.mark.parametrize("n", [0, MAX_BATCH + 1])
def test_batch_size_is_bounded(n):
    with pytest.raises(ValueError):
        asyncio.run(client(FakeJudge0([])).run_many("python", "pass", [""] * n))


def test_language_map_covers_the_supported_languages():
    assert set(LANGUAGES) == {"python", "javascript", "c", "cpp", "java", "go", "sql"}
    assert len({lang.judge0_id for lang in LANGUAGES.values()}) == len(LANGUAGES)
    for lang in LANGUAGES.values():  # every run must carry cpu / wall / memory limits
        lim = lang.limits
        assert 0 < lim.cpu_time_s <= lim.wall_time_s <= 15 and lim.memory_kb > 0

"""Run / Submit pass counts (FR-16, AC-5). Judge0 is replaced by a fake client: no network."""

import asyncio
import dataclasses

import pytest

from app.core.errors import AppError
from app.execution.judge0 import MAX_BATCH, RunResult
from app.execution.judging import Verdict
from app.execution.scoring import CaseSpec, SubmitResult, run_samples, submit


def ran(stdout="", status_id=3, status="Accepted", **kw) -> RunResult:
    base = dict(stderr="", compile_output="", message="", time_s=0.01, memory_kb=3000, exit_code=0)
    return RunResult(status_id=status_id, status=status, stdout=stdout, **{**base, **kw})


class FakeClient:
    """Stands in for Judge0Client: echoes `answer(stdin)` as stdout; records every call."""

    def __init__(self, answer=lambda stdin: stdin, override=None):
        self.answer, self.override = answer, override or {}
        self.calls: list[tuple[str, str, list[str]]] = []

    async def run_many(self, language, source, stdins):
        self.calls.append((language, source, list(stdins)))
        return [self.override.get(s) or ran(self.answer(s)) for s in stdins]


def cases(samples=3, hidden=15) -> list[CaseSpec]:
    # input "n" -> expected "n" (the fake client echoes), samples first like the real data
    sample = [CaseSpec(f"s{i}", f"s{i}", True) for i in range(samples)]
    secret = [CaseSpec(f"h{i}", f"h{i}", False) for i in range(hidden)]
    return sample + secret


# --- Run -------------------------------------------------------------------------------------


def test_run_executes_the_sample_cases_only():
    client = FakeClient()
    out = asyncio.run(run_samples(client, "python", "code", cases()))

    assert len(out) == 3 and all(j.passed for j in out)
    assert client.calls == [("python", "code", ["s0", "s1", "s2"])]  # hidden inputs never sent


def test_run_reports_each_sample_separately_in_order():
    client = FakeClient(override={"s1": ran("wrong"), "s2": ran(status_id=5, status="TLE")})
    out = asyncio.run(run_samples(client, "python", "code", cases()))
    assert [j.verdict for j in out] == [Verdict.PASSED, Verdict.WRONG_ANSWER, Verdict.TIME_LIMIT]


def test_run_with_no_samples_does_not_call_judge0():
    client = FakeClient()
    assert asyncio.run(run_samples(client, "python", "code", cases(samples=0))) == []
    assert client.calls == []


# --- Submit ----------------------------------------------------------------------------------


def test_submit_runs_all_18_and_counts_passes():
    client = FakeClient()
    result = asyncio.run(submit(client, "python", "code", cases()))

    assert (result.passed, result.total) == (18, 18)
    assert result.verdicts == {Verdict.PASSED: 18}
    assert len(client.calls) == 1 and len(client.calls[0][2]) == 18  # one batch, all inputs


def test_submit_counts_partial_passes_and_each_verdict():
    wrong = {f"h{i}": ran("nope") for i in range(0, 4)}  # 4 wrong answers
    slow = {f"h{i}": ran(status_id=5, status="TLE") for i in range(4, 6)}  # 2 time limits
    crash = {"h6": ran(status_id=11, status="NZEC", stderr="boom", exit_code=1)}
    client = FakeClient(override={**wrong, **slow, **crash})

    result = asyncio.run(submit(client, "python", "code", cases()))

    assert (result.passed, result.total) == (11, 18)
    assert result.verdicts == {
        Verdict.PASSED: 11,
        Verdict.WRONG_ANSWER: 4,
        Verdict.TIME_LIMIT: 2,
        Verdict.RUNTIME_ERROR: 1,
    }


def test_submit_compile_error_passes_nothing():
    err = ran(status_id=6, status="Compilation Error", compile_output="bad", exit_code=None)
    client = FakeClient(override={c.input: err for c in cases()})
    result = asyncio.run(submit(client, "c", "code", cases()))
    assert (result.passed, result.total) == (0, 18)
    assert result.verdicts == {Verdict.COMPILE_ERROR: 18}


def test_submit_with_no_cases_is_zero_of_zero():
    result = asyncio.run(submit(FakeClient(), "python", "code", []))
    assert (result.passed, result.total, result.verdicts) == (0, 0, {})


def test_submit_splits_a_large_case_list_into_batches():
    many = [CaseSpec(f"c{i}", f"c{i}", False) for i in range(MAX_BATCH + 5)]
    client = FakeClient()
    result = asyncio.run(submit(client, "python", "code", many))
    assert [len(c[2]) for c in client.calls] == [MAX_BATCH, 5]
    assert (result.passed, result.total) == (MAX_BATCH + 5, MAX_BATCH + 5)


def test_submit_result_exposes_counts_only():
    """AC-5 / D7: nothing about a hidden case can ride along in a Submit result."""
    fields = {f.name for f in dataclasses.fields(SubmitResult)}
    assert fields == {"passed", "total", "verdicts"}

    secret = "TOP-SECRET-EXPECTED-OUTPUT"
    hidden = [CaseSpec("secret-input", secret, False)]
    result = asyncio.run(submit(FakeClient(), "python", "code", hidden))
    assert "secret" not in repr(result).lower()


def test_judge0_outage_aborts_the_submit_instead_of_scoring_zero():
    err = AppError(503, "EXECUTION_UNAVAILABLE", "down")

    class Down:
        async def run_many(self, *a):
            raise err

    with pytest.raises(AppError) as e:
        asyncio.run(submit(Down(), "python", "code", cases()))
    assert e.value.code == "EXECUTION_UNAVAILABLE"


def test_judge0_internal_error_on_one_case_aborts_the_submit():
    client = FakeClient(override={"h3": ran(status_id=13, status="Internal Error")})
    with pytest.raises(AppError) as e:
        asyncio.run(submit(client, "python", "code", cases()))
    assert e.value.code == "EXECUTION_UNAVAILABLE"

"""Judging rules (FR-16). Pure functions over RunResult: no Judge0, no network."""

import pytest

from app.core.errors import AppError
from app.execution.judge0 import RunResult
from app.execution.judging import Verdict, judge, normalize_output, outputs_match
from app.execution.languages import LANGUAGES


def run(status_id=3, status="Accepted", **kw) -> RunResult:
    base = dict(
        stdout="",
        stderr="",
        compile_output="",
        message="",
        time_s=0.01,
        memory_kb=3000,
        exit_code=0,
    )
    return RunResult(status_id=status_id, status=status, **{**base, **kw})


# --- output comparison ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "actual, expected",
    [
        ("42", "42"),
        ("42\n", "42"),
        ("42", "42\n\n"),
        ("42\r\n", "42\n"),  # Windows line endings
        ("1 2 3  \n4 5 6\t\n", "1 2 3\n4 5 6"),  # trailing spaces / tabs on a line
        ("a\nb\n\n\n", "a\nb"),
        ("", "\n"),
    ],
)
def test_trailing_whitespace_is_ignored(actual, expected):
    assert outputs_match(actual, expected)


@pytest.mark.parametrize(
    "actual, expected",
    [
        (" 42", "42"),  # leading whitespace counts
        ("1  2", "1 2"),  # inner whitespace counts
        ("a\n\nb", "a\nb"),  # inner blank line counts
        ("\n42", "42"),  # leading blank line counts
        ("Yes", "yes"),  # case counts
        ("1\n2", "2\n1"),  # order counts (SQL rows too)
        ("42", ""),
    ],
)
def test_everything_but_trailing_whitespace_counts(actual, expected):
    assert not outputs_match(actual, expected)


def test_normalize_output_keeps_leading_and_inner_text():
    assert normalize_output("  a  b \n\n c\n\n") == "  a  b\n\n c"


# --- verdicts ---------------------------------------------------------------------------------


def test_matching_output_passes():
    j = judge("python", run(stdout="6\n"), "6")
    assert j.verdict is Verdict.PASSED and j.passed
    assert (j.stdout, j.detail, j.time_s, j.memory_kb) == ("6\n", "", 0.01, 3000)


def test_different_output_is_wrong_answer():
    j = judge("python", run(stdout="7\n"), "6")
    assert j.verdict is Verdict.WRONG_ANSWER and not j.passed


def test_no_output_is_wrong_answer():
    assert judge("python", run(), "6").verdict is Verdict.WRONG_ANSWER


def test_compile_error_carries_the_compiler_output():
    r = run(6, "Compilation Error", compile_output="main.c:1: error: x undeclared", exit_code=None)
    j = judge("c", r, "6")
    assert j.verdict is Verdict.COMPILE_ERROR and "x undeclared" in j.detail


def test_compile_error_falls_back_to_message():
    r = run(6, "Compilation Error", message="could not start compiler")
    assert judge("c", r, "").detail == "could not start compiler"


def test_time_limit():
    r = run(5, "Time Limit Exceeded", time_s=2.0, exit_code=None)
    j = judge("python", r, "6")
    assert j.verdict is Verdict.TIME_LIMIT and j.time_s == 2.0


def test_time_limit_wins_over_partial_matching_output():
    r = run(5, "Time Limit Exceeded", stdout="6\n")
    assert judge("python", r, "6").verdict is Verdict.TIME_LIMIT


@pytest.mark.parametrize(
    "status_id, status",
    [
        (7, "Runtime Error (SIGSEGV)"),
        (8, "Runtime Error (SIGXFSZ)"),
        (9, "Runtime Error (SIGFPE)"),
        (10, "Runtime Error (SIGABRT)"),
        (11, "Runtime Error (NZEC)"),
        (12, "Runtime Error (Other)"),
    ],
)
def test_runtime_errors_use_stderr(status_id, status):
    j = judge("python", run(status_id, status, stderr="Traceback: boom", exit_code=1), "6")
    assert j.verdict is Verdict.RUNTIME_ERROR and j.detail == "Traceback: boom"


def test_runtime_error_without_stderr_names_judge0_status():
    j = judge("c", run(7, "Runtime Error (SIGSEGV)", exit_code=139), "6")
    assert j.detail == "Runtime Error (SIGSEGV)"


def test_a_crash_after_printing_the_right_output_is_still_a_runtime_error():
    r = run(11, "Runtime Error (NZEC)", stdout="6\n", exit_code=1)
    assert judge("python", r, "6").verdict is Verdict.RUNTIME_ERROR


def test_memory_kill_is_a_runtime_error_labelled_memory_limit():
    limit = LANGUAGES["python"].limits.memory_kb
    r = run(11, "Runtime Error (NZEC)", exit_code=137, memory_kb=limit, stderr="Killed")
    j = judge("python", r, "6")
    assert j.verdict is Verdict.RUNTIME_ERROR and j.detail == "Memory limit exceeded"


def test_memory_limit_is_per_language():
    py_limit = LANGUAGES["python"].limits.memory_kb
    assert LANGUAGES["java"].limits.memory_kb > py_limit
    r = run(11, "Runtime Error (NZEC)", exit_code=137, memory_kb=py_limit, stderr="Killed")
    assert judge("java", r, "6").detail == "Killed"  # java's limit is higher: not a memory kill


def test_exit_137_below_the_memory_limit_is_a_plain_runtime_error():
    r = run(11, "Runtime Error (NZEC)", exit_code=137, memory_kb=5000, stderr="Killed")
    assert judge("python", r, "6").detail == "Killed"


def test_long_detail_is_truncated():
    r = run(11, "Runtime Error (NZEC)", stderr="x" * 10_000, exit_code=1)
    assert len(judge("python", r, "").detail) < 4100


@pytest.mark.parametrize("status_id, status", [(13, "Internal Error"), (14, "Exec Format Error")])
def test_judge0_internal_failures_are_not_the_students_fault(status_id, status):
    with pytest.raises(AppError) as e:
        judge("python", run(status_id, status), "6")
    assert (e.value.status, e.value.code) == (503, "EXECUTION_UNAVAILABLE")


def test_unknown_status_is_treated_as_unavailable():
    with pytest.raises(AppError) as e:
        judge("python", run(99, "Mystery"), "6")
    assert e.value.code == "EXECUTION_UNAVAILABLE"


def test_every_language_judges_through_the_same_rules():
    for key in LANGUAGES:
        assert judge(key, run(stdout="ok\n"), "ok").passed

"""Judging rules: turn what Judge0 ran (RunResult) into one verdict per test case (FR-16).

Judge0 only says "ran fine"; it never judges (we send no expected output). The rules here:

- Output is compared after normalising trailing whitespace only: line endings become "\\n",
  each line loses trailing spaces/tabs, trailing blank lines are dropped. Leading and inner
  whitespace, case and line order all still matter.
- compile error -> COMPILE_ERROR, time/wall limit -> TIME_LIMIT, any crash or non-zero exit
  -> RUNTIME_ERROR, a clean run with different output -> WRONG_ANSWER, same output -> PASSED.
- A kill for memory is Judge0 status 11 (NZEC) with exit 137 and memory at the limit. It is
  labelled RUNTIME_ERROR with the detail "Memory limit exceeded" (no fifth status for the UI).
- Judge0 internal errors are our fault, not the student's: they raise EXECUTION_UNAVAILABLE
  instead of being counted as a failed case.

Pure functions, no I/O. `stdout` of a hidden case must never reach a student: callers that
build responses for hidden cases use counts only (see CLAUDE.md in this folder).
"""

from dataclasses import dataclass
from enum import StrEnum

from app.core.errors import AppError
from app.execution.judge0 import ACCEPTED, COMPILATION_ERROR, TIME_LIMIT_EXCEEDED, RunResult
from app.execution.languages import LANGUAGES

MAX_DETAIL_CHARS = 4000  # compiler / stderr text sent back; keeps responses small

_NZEC = 11  # Judge0 "Runtime Error (NZEC)"
_SIGKILL_EXIT = 137  # 128 + 9: how a memory kill shows up
_RUNTIME_ERRORS = range(7, 13)  # SIGSEGV, SIGXFSZ, SIGFPE, SIGABRT, NZEC, other


class Verdict(StrEnum):
    PASSED = "passed"
    WRONG_ANSWER = "wrong_answer"
    COMPILE_ERROR = "compile_error"
    RUNTIME_ERROR = "runtime_error"
    TIME_LIMIT = "time_limit"


@dataclass(frozen=True)
class CaseJudgement:
    verdict: Verdict
    stdout: str
    detail: str  # compiler output / stderr / "Memory limit exceeded"; "" when nothing to say
    time_s: float | None
    memory_kb: int | None

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASSED


def normalize_output(text: str) -> str:
    """Trailing whitespace only: see the module docstring."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def outputs_match(actual: str, expected: str) -> bool:
    return normalize_output(actual) == normalize_output(expected)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_DETAIL_CHARS else text[:MAX_DETAIL_CHARS] + "\n[truncated]"


def _was_memory_kill(language: str, r: RunResult) -> bool:
    return (
        r.status_id == _NZEC
        and r.exit_code == _SIGKILL_EXIT
        and r.memory_kb is not None
        and r.memory_kb >= LANGUAGES[language].limits.memory_kb
    )


def judge(language: str, result: RunResult, expected_output: str) -> CaseJudgement:
    """Verdict for one test case. `language` is our key (it carries the memory limit)."""

    def out(verdict: Verdict, detail: str = "") -> CaseJudgement:
        return CaseJudgement(verdict, result.stdout, _clip(detail), result.time_s, result.memory_kb)

    sid = result.status_id
    if sid == COMPILATION_ERROR:
        return out(Verdict.COMPILE_ERROR, result.compile_output or result.message)
    if sid == TIME_LIMIT_EXCEEDED:
        return out(Verdict.TIME_LIMIT)
    if _was_memory_kill(language, result):
        return out(Verdict.RUNTIME_ERROR, "Memory limit exceeded")
    if sid in _RUNTIME_ERRORS:
        return out(Verdict.RUNTIME_ERROR, result.stderr or result.message or result.status)
    if sid == ACCEPTED:
        ok = outputs_match(result.stdout, expected_output)
        return out(Verdict.PASSED if ok else Verdict.WRONG_ANSWER)
    # Internal Error, Exec Format Error, or an id we do not know: not the student's fault.
    raise AppError(
        503,
        "EXECUTION_UNAVAILABLE",
        "Code execution is unavailable. Try again.",
        {"judge0_status": sid},
    )

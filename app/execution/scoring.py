"""Run and Submit on top of the judging rules (FR-16, AC-5).

- Run: executes the sample cases only and returns a result per case (students may see these).
- Submit: executes every case (3 sample + 15 hidden) and returns counts only: `passed` out of
  `total`, plus how many cases ended in each verdict. Nothing about a hidden case's input,
  expected output or stdout leaves this module; `SubmitResult` has no field that could hold it.

No database here: the caller loads the cases (hidden ones only in this path) and passes them in.
Level weighting of the score is an open decision, so the score is the plain pass count.
"""

from collections import Counter
from dataclasses import dataclass

from app.execution.judge0 import MAX_BATCH, Judge0Client
from app.execution.judging import CaseJudgement, Verdict, judge


@dataclass(frozen=True)
class CaseSpec:
    input: str
    expected: str
    is_sample: bool


@dataclass(frozen=True)
class SubmitResult:
    passed: int
    total: int
    verdicts: dict[Verdict, int]  # counts per verdict, e.g. {passed: 14, time_limit: 4}


async def _judge_all(
    client: Judge0Client, language: str, code: str, cases: list[CaseSpec]
) -> list[CaseJudgement]:
    """Judge every case, in order. Cases beyond one Judge0 batch go in further batches."""
    judged: list[CaseJudgement] = []
    for start in range(0, len(cases), MAX_BATCH):
        chunk = cases[start : start + MAX_BATCH]
        results = await client.run_many(language, code, [c.input for c in chunk])
        judged += [judge(language, r, c.expected) for r, c in zip(results, chunk, strict=True)]
    return judged


async def run_samples(
    client: Judge0Client, language: str, code: str, cases: list[CaseSpec]
) -> list[CaseJudgement]:
    """Run button: sample cases only, one judgement each, in case order."""
    samples = [c for c in cases if c.is_sample]
    return await _judge_all(client, language, code, samples) if samples else []


async def submit(
    client: Judge0Client, language: str, code: str, cases: list[CaseSpec]
) -> SubmitResult:
    """Submit button: every case counts. Returns counts only."""
    judged = await _judge_all(client, language, code, cases) if cases else []
    verdicts = Counter(j.verdict for j in judged)
    return SubmitResult(passed=verdicts[Verdict.PASSED], total=len(cases), verdicts=dict(verdicts))

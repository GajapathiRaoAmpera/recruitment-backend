"""AI drafts of Round 2 coding questions (POST /questions/generate).

The LLM writes the problem, a Python reference solution and the 18 stdin inputs. It never
writes the expected outputs: the reference solution is run on our Judge0 for every input and
its stdout becomes the expected output. A question whose reply is not schema-valid, or whose
reference solution fails on any input, is retried once with the error fed back, then dropped.

The NVIDIA client (key, retries, shared rate limit) is Round 1's, imported read-only from
app.papers.agents. API keys never reach logs, errors or responses.
"""

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.config import get_settings
from app.core.errors import AppError
from app.execution.coding_questions.schemas import (
    HIDDEN_CASES,
    MAX_BODY_CHARS,
    MAX_CASE_CHARS,
    SAMPLE_CASES,
    CaseIn,
)
from app.execution.judge0 import ACCEPTED, Judge0Client, RunResult
from app.papers.agents import LlmClient, LlmOutputError, NvidiaClient

log = logging.getLogger(__name__)

REFERENCE_LANGUAGE = "python"  # Judge0 image: Python 3.8
MAX_COUNT = 3
MAX_AVOID = 30  # this HR's most recent coding titles, passed as "do not repeat"
MAX_TITLE_CHARS = 120
MAX_INPUT_CHARS = 2_000  # the model types every input, so they stay small
MAX_SOLUTION_CHARS = 20_000
FEEDBACK_CHARS = 1_000  # stderr / input shown back to the model on a retry
REFERENCE_MAX_TIME_S = 2.5  # half the Python CPU limit: student solutions need headroom
ATTEMPTS = 2  # first try + one retry with the error fed back
BUDGET_S = 150.0  # whole request; unfinished questions fail with "timed out"
LLM_TIMEOUT_S = 120.0  # one HTTP attempt to NVIDIA
LLM_TEMPERATURE = 0.7  # varied problems, like Round 1's generator


# --------------------------------------------------------------------------------------------
# What the model must return
# --------------------------------------------------------------------------------------------


def _norm_title(title: str) -> str:
    return re.sub(r"\W+", " ", title.casefold()).strip()


class GeneratedCodingQuestion(BaseModel):
    """One model reply. Validated before anything runs or is saved."""

    model_config = ConfigDict(extra="ignore")  # models add chatter fields; they are dropped

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    statement: str = Field(min_length=1)
    reference_solution: str = Field(min_length=1, max_length=MAX_SOLUTION_CHARS)
    sample_inputs: list[str] = Field(min_length=SAMPLE_CASES, max_length=SAMPLE_CASES)
    hidden_inputs: list[str] = Field(min_length=HIDDEN_CASES, max_length=HIDDEN_CASES)

    @field_validator("title")
    @classmethod
    def _one_line_title(cls, v: str) -> str:
        v = v.strip()
        if not v or "\n" in v:
            raise ValueError("title must be one non-blank line")
        return v

    @field_validator("statement", "reference_solution")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip("\n")

    @field_validator("sample_inputs", "hidden_inputs")
    @classmethod
    def _inputs(cls, v: list[str]) -> list[str]:
        out = []
        for i, s in enumerate(v):
            if not s.strip():
                raise ValueError(f"input {i + 1} is blank")
            if len(s) > MAX_INPUT_CHARS:
                raise ValueError(f"input {i + 1} is longer than {MAX_INPUT_CHARS} characters")
            out.append(s if s.endswith("\n") else s + "\n")  # stdin ends with a newline
        return out

    @model_validator(mode="after")
    def _whole(self) -> "GeneratedCodingQuestion":
        if len(self.body) > MAX_BODY_CHARS:
            raise ValueError(f"title + statement is longer than {MAX_BODY_CHARS} characters")
        stripped = [s.strip() for s in self.inputs]
        if len(set(stripped)) != len(stripped):
            raise ValueError("all 18 inputs must be different")
        return self

    @property
    def body(self) -> str:
        return f"{self.title}\n{self.statement}"

    @property
    def inputs(self) -> list[str]:
        return [*self.sample_inputs, *self.hidden_inputs]


# --------------------------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------------------------

_LEVELS = {
    "easy": "EASY: one idea, a loop or two (counting, sums, simple string handling). "
    "A first-year student should solve it in 15 minutes.",
    "medium": "MEDIUM: needs a standard technique (sorting, hashing, two pointers, prefix sums, "
    "stack/queue, simple greedy or BFS). About 25 minutes.",
    "advanced": "ADVANCED: needs a real algorithm (dynamic programming, graphs, binary search on "
    "the answer, careful greedy proof). About 40 minutes.",
}

SYSTEM_PROMPT = f"""You write programming problems for a campus hiring coding test.
Students solve them in Python, JavaScript, C, C++, Java or Go. Their program reads stdin and
writes stdout, and the output is compared exactly (only trailing whitespace is ignored).

Write ONE original problem and reply with JSON only, exactly this shape:
{{"title": "...", "statement": "...", "reference_solution": "...",
  "sample_inputs": ["...", "...", "..."], "hidden_inputs": ["...", ... 15 strings]}}

Rules:
- title: short, one line, at most {MAX_TITLE_CHARS} characters.
- statement: plain text with these parts: the task, "Input format", "Output format",
  "Constraints". Do NOT include worked examples or expected outputs: the sample inputs are
  shown to students with outputs computed by running your solution.
- Every input has exactly ONE correct output. If the answer is a decimal, say exactly how to
  format it (for example "print with exactly 2 digits after the decimal point"). If several
  answers could be valid, add a rule that picks one (for example "print the smallest index").
- reference_solution: a correct, complete Python 3.8 program, standard library only. Read all
  of stdin, print the answer. No input prompts, no randomness, no files, no network.
  Nothing newer than Python 3.8: no match statements, no list[int] / dict[str, int] type
  hints, no str.removeprefix. It must finish in well under a second on every input.
- sample_inputs: exactly {SAMPLE_CASES} small, readable inputs that show the format.
- hidden_inputs: exactly {HIDDEN_CASES} inputs that test correctness: smallest values,
  edge cases (duplicates, negatives, zeros, all equal, already sorted, single element, as the
  problem allows), and a few larger ones.
- Every input is a plain JSON string literal holding the exact stdin text (use \\n between
  lines). You cannot run code: NEVER write expressions such as "..." + " ".join(...) or
  str(i) for i in range(...). Write every value out by hand, so even the largest inputs
  hold at most about 50 values. The reply must be valid JSON.
- Each input follows the input format and the constraints, is at most {MAX_INPUT_CHARS}
  characters, and all 18 inputs are different. The constraints in the statement must fit
  the inputs you write (for example N <= 50, values <= 10^6)."""


def build_user_prompt(
    *,
    difficulty: str,
    topic: str | None,
    index: int,
    count: int,
    avoid: Sequence[str],
    feedback: str | None = None,
) -> str:
    parts = [f"Difficulty: {_LEVELS[difficulty]}"]
    parts.append(f"Topic: {topic}" if topic else "Topic: any classic programming topic.")
    if count > 1:
        parts.append(
            f"This is problem {index + 1} of {count} written separately for the same test. "
            f"To keep them different, base it on the {_ordinal(index)} most natural kind of "
            "problem for this topic and difficulty, not the most obvious one each time."
        )
    if avoid:
        listed = "\n".join(f"- {t}" for t in avoid[:MAX_AVOID])
        parts.append(f"Do not repeat or closely copy these existing problems:\n{listed}")
    if feedback:
        parts.append(
            "Your previous reply could not be used:\n"
            f"{feedback}\nWrite the problem again (a new problem is fine) and fix this."
        )
    return "\n\n".join(parts)


def _ordinal(i: int) -> str:
    return ["first", "second", "third"][i] if i < 3 else f"#{i + 1}"


# --------------------------------------------------------------------------------------------
# One question: ask, validate, run, retry once
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Draft:
    """A question that passed every check, ready to save."""

    question: GeneratedCodingQuestion
    cases: list[CaseIn]


class QuestionFailed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _cut(text: str, n: int = FEEDBACK_CHARS) -> str:
    return text if len(text) <= n else text[:n] + " ...(cut)"


def _where(i: int) -> str:
    return f"sample input {i + 1}" if i < SAMPLE_CASES else f"hidden input {i - SAMPLE_CASES + 1}"


def check_runs(q: GeneratedCodingQuestion, runs: list[RunResult]) -> tuple[str, str] | None:
    """(reason for HR, feedback for the model) when the reference solution is not usable."""
    for i, r in enumerate(runs):
        where = _where(i)
        if r.status_id != ACCEPTED:
            err = r.compile_output or r.stderr or r.message
            return (
                f"reference solution failed on {where}: {r.status}",
                f"Your reference_solution failed on {where} with {r.status}.\n"
                f"Input:\n{_cut(q.inputs[i], 300)}\nError output:\n{_cut(err)}",
            )
        if not r.stdout.strip():
            return (
                f"reference solution printed nothing on {where}",
                f"Your reference_solution printed nothing on {where}.\n"
                f"Input:\n{_cut(q.inputs[i], 300)}",
            )
        if len(r.stdout) > MAX_CASE_CHARS:
            return (
                f"reference solution output on {where} is too long",
                f"Your reference_solution printed more than {MAX_CASE_CHARS} characters on "
                f"{where}. Keep outputs short.",
            )
        if r.time_s is not None and r.time_s > REFERENCE_MAX_TIME_S:
            return (
                f"reference solution is too slow on {where} ({r.time_s:.2f} s)",
                f"Your reference_solution took {r.time_s:.2f} s on {where}; it must finish in "
                "well under a second. Use a faster algorithm or smaller inputs.",
            )
    if len({r.stdout.rstrip() for r in runs}) < 2:
        return (
            "every input gives the same output",
            "Your reference_solution printed the same output for all 18 inputs, so the tests "
            "cannot tell right from wrong programs. Vary the inputs so the answers differ.",
        )
    return None


async def generate_one(
    llm: LlmClient,
    judge0: Judge0Client,
    *,
    difficulty: str,
    topic: str | None,
    index: int,
    count: int,
    avoid: Sequence[str],
) -> Draft:
    """Provider errors (AppError) propagate; bad output or a failing solution raises
    QuestionFailed after ATTEMPTS tries."""
    feedback: str | None = None
    reason = "no attempt made"
    for attempt in range(1, ATTEMPTS + 1):
        user = build_user_prompt(
            difficulty=difficulty,
            topic=topic,
            index=index,
            count=count,
            avoid=avoid,
            feedback=feedback,
        )
        try:
            raw = await llm.complete_json(system=SYSTEM_PROMPT, user=user)
        except LlmOutputError as e:
            reason = "model reply was not valid JSON"
            feedback = "Your reply was not a single valid JSON object. Reply with JSON only."
            cause = e.__cause__
            if isinstance(cause, json.JSONDecodeError):  # say where, so the model can fix it
                near = cause.doc[max(0, cause.pos - 120) : cause.pos + 60]
                feedback += f"\nJSON error: {cause.msg}, near: {near!r}"
            log.info("question %d attempt %d: %s", index, attempt, reason)
            continue
        try:
            q = GeneratedCodingQuestion.model_validate(raw)
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(map(str, err['loc'])) or 'reply'}: {err['msg']}" for err in e.errors()
            )
            reason = f"model reply failed validation: {_cut(problems, 300)}"
            feedback = f"Your JSON did not follow the rules: {_cut(problems)}"
            log.info("question %d attempt %d: invalid reply", index, attempt)
            continue
        try:
            runs = await judge0.run_many(REFERENCE_LANGUAGE, q.reference_solution, q.inputs)
        except AppError as e:
            if e.code != "EXECUTION_TIMEOUT":
                raise  # Judge0 down: the whole request stops
            reason = "Judge0 timed out running the reference solution"
            feedback = "Running your reference_solution on the 18 inputs took too long."
            log.info("question %d attempt %d: %s", index, attempt, reason)
            continue
        problem = check_runs(q, runs)
        if problem:
            reason, feedback = problem
            log.info("question %d attempt %d: %s", index, attempt, reason)
            continue
        cases = [
            CaseIn(input=stdin, expected=r.stdout, is_sample=i < SAMPLE_CASES)
            for i, (stdin, r) in enumerate(zip(q.inputs, runs, strict=True))
        ]
        return Draft(question=q, cases=cases)
    raise QuestionFailed(reason)


# --------------------------------------------------------------------------------------------
# Several questions in parallel, under one time budget
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    index: int
    reason: str
    error: AppError | None = None  # set when the AI provider itself failed


async def generate_many(
    llm: LlmClient,
    judge0: Judge0Client,
    *,
    difficulty: str,
    topic: str | None,
    count: int,
    avoid: Sequence[str],
    budget_s: float = BUDGET_S,
) -> tuple[list[Draft], list[Failure]]:
    """Generate `count` questions in parallel. Returns the good ones (in request order, title
    duplicates dropped) and one Failure per question that did not make it. Judge0 being
    unavailable raises (AppError 503) and nothing is returned."""
    deadline = asyncio.get_running_loop().time() + budget_s

    async def one(i: int) -> Draft | Failure:
        try:
            async with asyncio.timeout_at(deadline):
                return await generate_one(
                    llm,
                    judge0,
                    difficulty=difficulty,
                    topic=topic,
                    index=i,
                    count=count,
                    avoid=avoid,
                )
        except QuestionFailed as e:
            return Failure(i, e.reason)
        except TimeoutError:
            return Failure(i, f"timed out after {budget_s:.0f} s")
        except AppError as e:
            if e.code.startswith("AI_"):  # provider error / rate limit for this question only
                return Failure(i, e.message, e)
            raise

    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(one(i)) for i in range(count)]
    except* AppError as group:  # Judge0 unavailable: siblings were cancelled
        raise group.exceptions[0] from None

    drafts: list[Draft] = []
    failures: list[Failure] = []
    seen = {_norm_title(t) for t in avoid}
    for i, task in enumerate(tasks):
        result = task.result()
        if isinstance(result, Failure):
            failures.append(result)
        elif _norm_title(result.question.title) in seen:
            failures.append(Failure(i, f"duplicate title: {result.question.title}"))
        else:
            seen.add(_norm_title(result.question.title))
            drafts.append(result)
    return drafts, failures


# --------------------------------------------------------------------------------------------
# Clients (FastAPI dependencies; tests override them)
# --------------------------------------------------------------------------------------------


def generation_llm() -> LlmClient:
    """503 AI_PROVIDER_NOT_CONFIGURED when NVIDIA_API_KEY is not set."""
    return NvidiaClient(
        get_settings().nvidia_api_key or "", temperature=LLM_TEMPERATURE, timeout=LLM_TIMEOUT_S
    )


# Judge0: the shared client (app.execution.deps.judge0_client), same limits and 15 s timeout.

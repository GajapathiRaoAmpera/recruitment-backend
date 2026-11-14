"""LLM layer of the Round 1 paper builder: an NVIDIA model writes the Technical questions.

Every model reply is schema-validated (GeneratedMcq) before use. No model checks the answers:
HR reviews and edits every question before a paper is accepted, so LLM questions carry
verified=False. One provider call per batch, and every request waits for a shared slot, so a
paper stays under the free-tier rate limit.

API keys are read from settings, sent only in request headers, and never logged or put in errors.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from app.core.config import get_settings
from app.core.errors import AppError
from app.papers.builder import needed, split_by_percent
from app.papers.schemas import LEVELS, SECTIONS, Level, PaperSettings, PoolQuestion, Section, Track

log = logging.getLogger(__name__)

BATCH_SIZE = 8  # questions per generator call
MAX_CONCURRENT_BATCHES = 3
MAX_AVOID = 60  # most recent accepted texts passed to the generator as "do not repeat"

Sleep = Callable[[float], Awaitable[None]]
OnProgress = Callable[[int, int], Awaitable[None]]


# --------------------------------------------------------------------------------------------
# Prompts. Defaults live here; an `ai_prompts` row (key -> body) overrides them so Super Admin
# can tune wording later. The service layer passes those rows in as `overrides`.
# --------------------------------------------------------------------------------------------

PROMPT_GEN_PREFIX = "paper_gen_"  # paper_gen_aptitude | paper_gen_reasoning | paper_gen_technical
PROMPT_COMPOSE = "paper_compose"
COMPOSE_TEXT_CHARS = 200  # stem characters the composer sees per candidate

_JSON_SHAPE = (
    "Reply with JSON only, exactly this shape:\n"
    '{"questions": [{"text": "...", "options": ["...", "...", "...", "..."], "answer": 0, '
    '"why": "...", "topic": "...", "fixed_options": false}]}\n'
    "- options: exactly 4 distinct, non-empty strings. Never use 'All of the above', "
    "'None of the above' or 'Both A and B'.\n"
    "- answer: the 0-based index (0-3) of the single correct option.\n"
    "- why: the explanation shown to reviewers.\n"
    "- fixed_options: true only when the option order carries meaning and must not be shuffled "
    "(e.g. syllogism conclusions like 'Only I follows', 'Only II follows', 'Either I or II "
    "follows', 'Neither I nor II follows'); otherwise false."
)

DEFAULT_PROMPTS: dict[str, str] = {
    f"{PROMPT_GEN_PREFIX}aptitude": (
        "You write quantitative aptitude multiple-choice questions for a campus recruitment "
        "screening test. Every question must have exactly one correct answer that you have "
        "computed yourself. The `why` field must be a complete worked solution, step by step, "
        "ending with the final value. Distractors must be plausible: values a candidate gets "
        "from common slips (wrong formula, arithmetic slip, forgetting a step, wrong unit). "
        "Keep numbers clean enough to solve without a calculator.\n\n" + _JSON_SHAPE
    ),
    f"{PROMPT_GEN_PREFIX}reasoning": (
        "You write logical reasoning multiple-choice questions for a campus recruitment "
        "screening test (series, coding-decoding, blood relations, direction sense, seating "
        "arrangement, syllogisms, statements and conclusions, analogies). Every question must "
        "have exactly one correct answer that you have derived yourself. The `why` field must "
        "be a complete worked solution showing each deduction. Distractors must be plausible: "
        "answers reached through common reasoning slips. Syllogism-style questions use the "
        "standard fixed conclusion options and set fixed_options to true.\n\n" + _JSON_SHAPE
    ),
    f"{PROMPT_GEN_PREFIX}technical": (
        "You write technical multiple-choice questions for a campus recruitment screening test. "
        "Follow the track rules in the request exactly. Every question must have exactly one "
        "correct answer. The `why` field explains why the correct option is right and why the "
        "others are wrong. Put any code inside fenced code blocks (```language ... ```) in the "
        "question text, with real newlines.\n\n" + _JSON_SHAPE
    ),
    PROMPT_COMPOSE: (
        "You assemble the question pool for a campus recruitment screening test. You get "
        "candidate questions (id, level, section, topic, start of the text) grouped into cells "
        "of level/section, and how many to keep in each cell. Keep exactly that many per cell. "
        "Cover the topics of a cell as evenly as you can and drop near-duplicates (questions "
        "that test the same fact or differ only in numbers or wording).\n"
        'Reply with JSON only, exactly this shape: {"keep": ["<id>", "<id>", ...]}'
    ),
}

LEVEL_RUBRIC: dict[Level, str] = {
    "easy": "easy: solvable in one step; a direct application of one idea or formula.",
    "medium": "medium: needs two steps or two ideas combined.",
    "advanced": (
        "advanced: multi-step, or turns on an edge case / subtle detail a careless candidate "
        "would miss."
    ),
}

STYLE_RULES: dict[str, str] = {
    "aptitude": "Quantitative aptitude. Include a worked solution in `why`.",
    "reasoning": "Logical reasoning. Include a worked solution in `why`.",
    "concept": (
        "IT technical, CONCEPT questions: test understanding of a programming / CS concept "
        "(behaviour, complexity, definitions applied to a scenario)."
    ),
    "syntax": (
        "IT technical, SYNTAX questions: mix these kinds - predict the output of a code snippet, "
        "find the bug in a snippet, fill in the missing keyword, pick the syntactically correct "
        "statement. Put every snippet in a fenced code block with the language name."
    ),
    "soft_skill": (
        "Non-IT technical, ROLE KNOWLEDGE and SOFT SKILLS questions: practical knowledge the "
        "stated role uses on the job, situational judgement at work, choosing the best response "
        "to a customer or colleague, and choosing the most appropriate message tone. No "
        "programming. Exactly one option must be clearly the best professional response; `why` "
        "gives the rationale."
    ),
}

TOPICS: dict[str, tuple[str, ...]] = {
    "aptitude": (
        "percentages",
        "profit and loss",
        "ratio and proportion",
        "time and work",
        "time, speed and distance",
        "simple and compound interest",
        "averages",
        "mixtures and alligation",
        "number system",
        "permutations and probability",
    ),
    "reasoning": (
        "number and letter series",
        "coding-decoding",
        "blood relations",
        "direction sense",
        "seating arrangement",
        "syllogisms",
        "statements and conclusions",
        "analogies and odd one out",
    ),
    "concept": (
        "data structures",
        "algorithms and complexity",
        "object-oriented programming",
        "databases and SQL",
        "operating systems",
        "computer networks",
    ),
    "syntax": ("Python", "Java", "C", "SQL", "JavaScript"),
    "soft_skill": (
        "customer handling",
        "teamwork and conflict",
        "time management and prioritisation",
        "workplace communication and tone",
        "professional ethics",
        "problem solving at work",
    ),
}


def get_prompt(key: str, overrides: Mapping[str, str] | None = None) -> str:
    """Prompt body for `key`: a non-blank override (ai_prompts row) wins, else the default."""
    if overrides:
        body = overrides.get(key)
        if body and body.strip():
            return body
    try:
        return DEFAULT_PROMPTS[key]
    except KeyError:
        raise ValueError(f"unknown prompt key: {key}") from None


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def build_generation_prompt(
    *,
    track: Track,
    level: Level,
    section: Section,
    style: str,
    topic: str,
    count: int,
    avoid: Sequence[str] = (),
    overrides: Mapping[str, str] | None = None,
    role: str | None = None,
) -> tuple[str, str]:
    system = get_prompt(f"{PROMPT_GEN_PREFIX}{section}", overrides)
    track_name = "IT (software / computer science)" if track == "it" else "Non-IT"
    lines = [
        f"Write exactly {count} multiple-choice question(s).",
        f"Track: {track_name}. Section: {section}. Topic: {topic}.",
        f"Difficulty - {LEVEL_RUBRIC[level]}",
        f"Style: {STYLE_RULES[style]}",
        "Each question must be self-contained and different from the others.",
    ]
    if role and section == "technical":
        lines.insert(
            2,
            f"Role being hired: {role}. Test the knowledge this role uses on the job. If the "
            "topic above is not something this role works with, use the closest one it does.",
        )
    if avoid:
        recent = list(avoid)[-MAX_AVOID:]
        lines.append("Do not repeat or paraphrase any of these existing questions:")
        lines.extend(f"- {' '.join(t.split())[:200]}" for t in recent)
    return system, "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Output schema
# --------------------------------------------------------------------------------------------

_BANNED_OPTIONS = ("all of the above", "none of the above", "both a and b", "all of these")


class GeneratedMcq(BaseModel):
    text: str
    options: list[str]
    answer: int
    why: str
    topic: str
    fixed_options: bool = False

    @field_validator("answer", mode="before")
    @classmethod
    def _int_not_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("answer must be an integer index")
        return v

    @model_validator(mode="after")
    def _check(self) -> "GeneratedMcq":
        self.text = self.text.strip()
        self.why = self.why.strip()
        self.topic = self.topic.strip()
        self.options = [o.strip() for o in self.options]
        if not self.text:
            raise ValueError("text must not be blank")
        if not self.why:
            raise ValueError("why must not be blank")
        if not self.topic:
            raise ValueError("topic must not be blank")
        if len(self.options) != 4:
            raise ValueError("exactly 4 options are required")
        if any(not o for o in self.options):
            raise ValueError("options must not be blank")
        if len({_norm(o) for o in self.options}) != 4:
            raise ValueError("options must be distinct")
        if any(_norm(o).rstrip(".") in _BANNED_OPTIONS for o in self.options):
            raise ValueError("'all/none of the above' style options are not allowed")
        if not 0 <= self.answer < 4:
            raise ValueError("answer must be an index from 0 to 3")
        return self


# --------------------------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------------------------


class LlmOutputError(Exception):
    """The provider answered, but not with usable JSON (treated as a rejected question)."""


class LlmClient(Protocol):
    async def complete_json(self, *, system: str, user: str) -> dict: ...


def parse_json_object(text: str) -> dict:
    """Parse a model reply as a JSON object, tolerating a ```json fence around it."""
    s = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", s, flags=re.DOTALL)
    if fenced:
        s = fenced.group(1)
    try:
        value = json.loads(s)
    except ValueError as e:
        raise LlmOutputError("model reply is not valid JSON") from e
    if not isinstance(value, dict):
        raise LlmOutputError("model reply is not a JSON object")
    return value


def _not_configured(provider: str) -> AppError:
    return AppError(
        503,
        "AI_PROVIDER_NOT_CONFIGURED",
        f"The {provider} API key is not configured on the server.",
        {"provider": provider.lower()},
    )


def _provider_error(provider: str, status: int | None) -> AppError:
    details: dict[str, Any] = {"provider": provider.lower()}
    if status is not None:
        details["provider_status"] = status
    return AppError(
        502, "AI_PROVIDER_ERROR", f"{provider} could not complete the request. Try again.", details
    )


def _rate_limited(provider: str) -> AppError:
    return AppError(
        429,
        "AI_PROVIDER_RATE_LIMITED",
        f"{provider} is rate-limiting requests. Wait a minute and try again, or generate fewer "
        "questions (lower the counts or the pool factor).",
        {"provider": provider.lower(), "provider_status": 429},
    )


class _HttpJsonClient:
    """Shared POST + retry. 5xx/network errors: 3 short tries. 429: up to 6 tries, waiting as
    long as the provider asks (Retry-After header), capped at 60 s."""

    provider = "LLM"
    max_attempts = 3
    rate_limit_attempts = 6
    base_delay = 1.0
    rate_limit_base_delay = 5.0  # provider windows are per minute; 1-2 s retries just burn tries
    max_delay = 60.0

    def __init__(
        self,
        api_key: str | None,
        model: str,
        *,
        sleep: Sleep = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 90.0,
    ):
        if not api_key or not api_key.strip():
            raise _not_configured(self.provider)
        self._api_key = api_key.strip()
        self.model = model
        self._sleep = sleep
        self._transport = transport
        self._timeout = httpx.Timeout(timeout, connect=10.0)

    def __repr__(self) -> str:  # never show the key
        return f"{type(self).__name__}(model={self.model!r})"

    def _delay(self, attempt: int, r: httpx.Response | None) -> float:
        if r is not None:
            try:
                return min(max(float(r.headers.get("retry-after", "")), 0.0), self.max_delay)
            except ValueError:
                pass
        limited = r is not None and r.status_code == 429
        base = self.rate_limit_base_delay if limited else self.base_delay
        return min(base * 2 ** (attempt - 1), self.max_delay)

    async def _throttle(self) -> None:
        """Wait for a request slot (providers with a per-minute limit override this)."""

    async def _post(self, url: str, headers: dict[str, str], body: dict) -> httpx.Response:
        last_status: int | None = None
        rate_limited = False
        attempt = 0
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            while True:
                attempt += 1
                r: httpx.Response | None = None
                await self._throttle()
                try:
                    r = await client.post(url, headers=headers, json=body)
                except httpx.HTTPError as e:
                    # type name only: some httpx messages include the request URL
                    log.warning("%s request failed (%s)", self.provider, type(e).__name__)
                    last_status = None
                else:
                    if r.status_code == 429:
                        rate_limited, last_status = True, 429
                    elif r.status_code >= 500:
                        last_status = r.status_code
                    else:
                        return r
                limit = self.rate_limit_attempts if rate_limited else self.max_attempts
                if attempt >= limit:
                    break
                wait = self._delay(attempt, r)
                log.warning(
                    "%s returned %s, attempt %d/%d, retrying in %.0f s",
                    self.provider,
                    f"HTTP {last_status}" if last_status else "no response",
                    attempt,
                    limit,
                    wait,
                )
                await self._sleep(wait)
        log.warning("%s gave up after %d attempts", self.provider, attempt)
        if last_status == 429:
            raise _rate_limited(self.provider)
        raise _provider_error(self.provider, last_status)

    def _fail(self, r: httpx.Response) -> AppError:
        log.warning("%s returned HTTP %d", self.provider, r.status_code)  # no body: may echo input
        return _provider_error(self.provider, r.status_code)


_next_slot = 0.0  # monotonic time of the next free request slot, shared by every NvidiaClient


class NvidiaClient(_HttpJsonClient):
    """NVIDIA NIM (build.nvidia.com) OpenAI-compatible chat completions.

    One key serves every model, and the free tier limits the KEY (about 40 requests a minute), so
    all instances share one slot clock: requests start at least 60 / nvidia_rpm seconds apart.
    """

    provider = "NVIDIA"
    url = "https://integrate.api.nvidia.com/v1/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        temperature: float = 0.0,
        rpm: int | None = None,
        **kw: Any,
    ):
        s = get_settings()
        kw.setdefault("timeout", 180.0)  # large models, a batch of questions, no streaming
        super().__init__(
            api_key if api_key is not None else s.nvidia_api_key,
            model or s.nvidia_generator_model,
            **kw,
        )
        self.temperature = temperature
        self._interval = 60.0 / max(rpm if rpm is not None else s.nvidia_rpm, 1)

    async def _throttle(self) -> None:
        # shortcut: per-process limiter, move to a shared store if the API runs more than one worker
        global _next_slot
        now = time.monotonic()
        start = max(now, _next_slot)
        _next_slot = start + self._interval  # reserved before any await, so no lock is needed
        if start > now:
            await self._sleep(start - now)

    async def complete_json(self, *, system: str, user: str) -> dict:
        r = await self._post(
            self.url,
            {"Authorization": f"Bearer {self._api_key}"},
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                # no response_format: not every NIM model accepts it; replies are parsed and
                # schema-validated here instead
                "temperature": self.temperature,
                "max_tokens": 16384,  # the composer lists every kept id; the default cap is small
                # reasoning models otherwise spend the whole budget thinking and return no content
                # (measured: 4096 reasoning tokens, content null); off is also 3x faster
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        if r.is_error:
            raise self._fail(r)
        try:
            text = r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LlmOutputError("NVIDIA reply had no message content") from e
        if not isinstance(text, str):
            raise LlmOutputError("NVIDIA reply had no message content")
        return parse_json_object(text)


def providers_status() -> dict[str, bool]:
    return {"nvidia": bool(get_settings().nvidia_api_key)}


# --------------------------------------------------------------------------------------------
# Generate
# --------------------------------------------------------------------------------------------


@dataclass
class BatchResult:
    accepted: list[PoolQuestion] = field(default_factory=list)
    rejected: int = 0


@dataclass(frozen=True)
class BatchPlan:
    level: Level
    section: Section
    style: str
    topic: str
    count: int


def _parse_questions(raw: Any, topic: str) -> list[GeneratedMcq]:
    """The schema-valid questions in a generator reply; anything else is dropped."""
    items = raw.get("questions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    valid: list[GeneratedMcq] = []
    for item in items:
        if isinstance(item, dict) and not item.get("topic"):
            item = {**item, "topic": topic}
        try:
            valid.append(GeneratedMcq.model_validate(item))
        except ValidationError:
            log.info("generator returned an invalid question; rejected")
    return valid


async def generate_batch(
    generator: LlmClient,
    *,
    track: Track,
    level: Level,
    section: Section,
    style: str,
    topic: str,
    count: int,
    avoid: Sequence[str],
    overrides: Mapping[str, str] | None = None,
    role: str | None = None,
) -> BatchResult:
    """Generate `count` (<= BATCH_SIZE) questions in ONE provider call; keep the schema-valid,
    non-duplicate ones.

    Provider failures (AppError) propagate; bad model output only rejects questions.
    """
    if not 1 <= count <= BATCH_SIZE:
        raise ValueError(f"count must be between 1 and {BATCH_SIZE}")
    result = BatchResult()
    seen = {_norm(t) for t in avoid}

    async def ask(n: int) -> tuple[list[GeneratedMcq], int]:
        system, user = build_generation_prompt(
            track=track,
            level=level,
            section=section,
            style=style,
            topic=topic,
            count=n,
            avoid=[*avoid, *(q.text for q in result.accepted)],
            overrides=overrides,
            role=role,
        )
        try:
            raw = await generator.complete_json(system=system, user=user)
        except LlmOutputError:
            return [], n
        valid = _parse_questions(raw, topic)[:n]
        return valid, n - len(valid)  # invalid, missing and malformed all count as rejected

    def fresh(q: GeneratedMcq) -> bool:
        key = _norm(q.text)
        if key in seen:
            return False
        seen.add(key)
        return True

    def accept(q: GeneratedMcq) -> None:
        result.accepted.append(
            PoolQuestion(
                id=str(uuid.uuid4()),  # becomes questions.id on send
                level=level,
                section=section,
                topic=q.topic,
                style=style,
                text=q.text,
                options=q.options,
                answer=q.answer,
                why=q.why,
                fixed_options=q.fixed_options,
                verified=False,  # no model checks the key: HR reviews it
            )
        )

    questions, bad = await ask(count)
    result.rejected += bad
    for q in questions:
        if fresh(q):
            accept(q)
        else:
            result.rejected += 1
    return result


def _styles(settings: PaperSettings, section: Section, count: int) -> dict[str, int]:
    if section != "technical":
        return {section: count}
    if settings.track == "non_it":
        return {"soft_skill": count}
    p = settings.it_syntax_percent
    if p in (0, 100):
        return {"syntax" if p == 100 else "concept": count}
    return split_by_percent(count, {"syntax": p, "concept": 100 - p})


def _cell_plans(
    settings: PaperSettings, level: Level, section: Section, count: int, topic_offset: int = 0
) -> list[BatchPlan]:
    plans: list[BatchPlan] = []
    for style, n in _styles(settings, section, count).items():
        topics = TOPICS[style]
        i = topic_offset
        while n > 0:
            size = min(BATCH_SIZE, n)
            plans.append(BatchPlan(level, section, style, topics[i % len(topics)], size))
            n -= size
            i += 1
    return plans


def _wanted(
    settings: PaperSettings, sections: Sequence[Section]
) -> dict[Level, dict[Section, int]]:
    """needed(settings) with the sections outside `sections` set to 0."""
    return {
        lv: {s: (n if s in sections else 0) for s, n in cells.items()}
        for lv, cells in needed(settings).items()
    }


def plan_batches(
    settings: PaperSettings, sections: Sequence[Section] = SECTIONS
) -> list[BatchPlan]:
    """Batches (<= BATCH_SIZE) covering needed(settings) x pool_factor, topics rotated."""
    want = _wanted(settings, sections)
    return [
        plan
        for level in LEVELS
        for section in SECTIONS
        for plan in _cell_plans(
            settings, level, section, want[level][section] * settings.pool_factor
        )
    ]


async def generate_pool(
    generator: LlmClient,
    settings: PaperSettings,
    *,
    on_progress: OnProgress,
    overrides: Mapping[str, str] | None = None,
    sections: Sequence[Section] = SECTIONS,
) -> list[PoolQuestion]:
    """Build the pool: needed x pool_factor per level+section, at most 3 batches at once.
    `sections` limits generation to those sections (the others are left to other sources).

    on_progress(done, total) drives a progress bar, so it never moves backwards: `total` is
    fixed from the first call and counts every planned question plus a reserve of one unit per
    level+section the paper needs (the optional top-up round is scaled into that reserve). On
    success the last call is (total, total). Raises POOL_TOO_SMALL (422) when the survivors
    cannot cover the paper itself.
    """
    want = _wanted(settings, sections)
    plans = plan_batches(settings, sections)
    planned = sum(p.count for p in plans)
    reserve = sum(1 for level in LEVELS for section in SECTIONS if want[level][section] > 0)
    total = planned + reserve
    done = 0
    topup_total = topup_done = 0  # questions asked / finished in the top-up round
    pool: list[PoolQuestion] = []
    texts: set[str] = set()
    lock = asyncio.Lock()
    gate = asyncio.Semaphore(MAX_CONCURRENT_BATCHES)

    async def report(new_done: int) -> None:
        nonlocal done
        done = min(max(done, new_done), total)
        await on_progress(done, total)

    async def run(plan: BatchPlan, top_up: bool) -> None:
        nonlocal topup_done
        async with gate:
            res = await generate_batch(
                generator,
                track=settings.track,
                level=plan.level,
                section=plan.section,
                style=plan.style,
                topic=plan.topic,
                count=plan.count,
                avoid=[q.text for q in pool if q.section == plan.section],
                overrides=overrides,
                role=settings.role,
            )
        async with lock:
            for q in res.accepted:  # concurrent batches can still collide on text
                key = _norm(q.text)
                if key not in texts:
                    texts.add(key)
                    pool.append(q)
            if top_up:
                topup_done += plan.count
                await report(planned + topup_done * reserve // topup_total)
            else:
                await report(done + plan.count)

    async def run_all(batch: list[BatchPlan], top_up: bool = False) -> None:
        tasks = [asyncio.create_task(run(p, top_up)) for p in batch]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    await report(0)
    await run_all(plans)

    def have(level: Level, section: Section) -> int:
        return sum(1 for q in pool if q.level == level and q.section == section)

    # one top-up round for cells that fell short of the paper itself
    topups = [
        plan
        for level in LEVELS
        for section in SECTIONS
        if (short := want[level][section] - have(level, section)) > 0
        for plan in _cell_plans(settings, level, section, short, topic_offset=1)
    ]
    if topups:
        topup_total = sum(p.count for p in topups)
        await run_all(topups, top_up=True)

    missing = {
        f"{level}/{section}": want[level][section] - have(level, section)
        for level in LEVELS
        for section in SECTIONS
        if have(level, section) < want[level][section]
    }
    if missing:
        raise AppError(
            422,
            "POOL_TOO_SMALL",
            "The AI returned too few usable questions to build the paper. Try generating again.",
            {"missing": missing, "pool_size": len(pool)},
        )
    if done < total:
        await report(total)  # no top-up needed: the reserve is simply complete
    return pool


# --------------------------------------------------------------------------------------------
# Composer: one call that picks which candidates of each level/section cell are selected
# --------------------------------------------------------------------------------------------


def _compose_prompt(
    sent: Sequence[PoolQuestion],
    target: Mapping[tuple[Level, Section], int],
    counts: Mapping[tuple[Level, Section], int],
    overrides: Mapping[str, str] | None,
    problem: str | None,
) -> tuple[str, str]:
    """No options, answers or explanations: only id, cell, topic and the start of the stem."""
    lines = ["Keep exactly:"]
    lines += [f"- {lv}/{s}: keep {target[(lv, s)]} of {counts[(lv, s)]}" for lv, s in counts]
    lines.append("Candidates (one JSON object per line):")
    lines += [
        json.dumps(
            {
                "id": q.id,
                "level": q.level,
                "section": q.section,
                "topic": q.topic,
                "text": " ".join(q.text.split())[:COMPOSE_TEXT_CHARS],
            },
            ensure_ascii=False,
        )
        for q in sent
    ]
    if problem:
        lines.append(f"Your previous reply was rejected: {problem}. Follow the counts exactly.")
    return get_prompt(PROMPT_COMPOSE, overrides), "\n".join(lines)


def _compose_problem(
    raw: Any,
    cell_of: Mapping[str, tuple[Level, Section]],
    target: Mapping[tuple[Level, Section], int],
) -> str | None:
    keep = raw.get("keep") if isinstance(raw, dict) else None
    if not isinstance(keep, list) or not all(isinstance(i, str) for i in keep):
        return 'the reply must be {"keep": [<id strings>]}'
    if len(set(keep)) != len(keep):
        return "ids were repeated"
    unknown = [i for i in keep if i not in cell_of]
    if unknown:
        return f"{len(unknown)} id(s) are not candidates"
    got: dict[tuple[Level, Section], int] = {}
    for i in keep:
        got[cell_of[i]] = got.get(cell_of[i], 0) + 1
    wrong = [
        f"{lv}/{s}: {got.get((lv, s), 0)} instead of {n}"
        for (lv, s), n in target.items()
        if got.get((lv, s), 0) != n
    ]
    return "wrong counts (" + "; ".join(wrong) + ")" if wrong else None


async def compose_selection(
    client: LlmClient,
    candidates: Sequence[PoolQuestion],
    target: Mapping[tuple[Level, Section], int],
    *,
    overrides: Mapping[str, str] | None = None,
) -> set[str]:
    """Ids to keep: exactly target[(level, section)] per cell. Cells kept whole (or emptied) need
    no model; the rest go to ONE call, validated by code, retried once, else 502
    AI_COMPOSE_INVALID. Provider errors (AppError) propagate without a retry."""
    by_cell: dict[tuple[Level, Section], list[PoolQuestion]] = {}
    for q in candidates:
        by_cell.setdefault((q.level, q.section), []).append(q)
    keep: set[str] = set()
    choose: dict[tuple[Level, Section], int] = {}
    for cell, n in target.items():
        have = by_cell.get(cell, [])
        if n > len(have):
            raise ValueError(f"target {n} for {cell} exceeds its {len(have)} candidates")
        if n == len(have):
            keep.update(q.id for q in have)
        elif n > 0:
            choose[cell] = n
    if not choose:
        return keep
    sent = [q for cell in choose for q in by_cell[cell]]
    cell_of = {q.id: (q.level, q.section) for q in sent}
    counts = {cell: len(by_cell[cell]) for cell in choose}
    problem: str | None = None
    for _ in range(2):
        system, user = _compose_prompt(sent, choose, counts, overrides, problem)
        try:
            raw = await client.complete_json(system=system, user=user)
        except LlmOutputError:
            problem = "it was not valid JSON"
            continue
        problem = _compose_problem(raw, cell_of, choose)
        if problem is None:
            return keep | set(raw["keep"])
    log.warning("composer reply invalid twice (%s)", problem)
    raise AppError(
        502,
        "AI_COMPOSE_INVALID",
        "The AI could not pick the sample paper questions. Try generating again.",
        {"reason": problem},
    )

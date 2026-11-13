"""LLM agents: fake LlmClients only (plus httpx.MockTransport for the HTTP clients). No network."""

import asyncio
import itertools
import json
import logging
import re

import httpx
import pytest

from app.core.config import get_settings
from app.core.errors import AppError
from app.papers import agents
from app.papers.agents import (
    NvidiaClient,
    generate_batch,
    generate_pool,
    get_prompt,
    parse_json_object,
    providers_status,
)
from app.papers.builder import needed
from app.papers.schemas import LEVELS, SECTIONS, PaperSettings

SECRET = "sk-super-secret-test-key-123"
_ids = itertools.count()


def mcq(text: str | None = None, answer: int = 1, **kw) -> dict:
    n = next(_ids)
    q = {
        "text": text or f"Question number {n}?",
        "options": [f"opt {n} a", f"opt {n} b", f"opt {n} c", f"opt {n} d"],
        "answer": answer,
        "why": f"Worked solution {n}: step one, step two.",
        "topic": "percentages",
    }
    q.update(kw)
    return q


class ScriptedGen:
    """Returns the scripted replies in order, then valid fresh questions."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []
        self.answers: dict[str, int] = {}

    async def complete_json(self, *, system: str, user: str) -> dict:
        self.calls.append((system, user))
        if self.replies:
            reply = self.replies.pop(0)
        else:
            count = int(re.search(r"Write exactly (\d+)", user).group(1))
            reply = {"questions": [mcq() for _ in range(count)]}
        if isinstance(reply, Exception):
            raise reply
        for q in reply.get("questions", []) if isinstance(reply, dict) else []:
            if isinstance(q, dict) and "text" in q and isinstance(q.get("answer"), int):
                self.answers[" ".join(q["text"].split())] = q["answer"]
        return reply


def run(coro):
    return asyncio.run(coro)


def batch(gen, count=3, avoid=(), **kw):
    params = dict(
        track="it",
        level="easy",
        section="aptitude",
        style="aptitude",
        topic="percentages",
        count=count,
        avoid=list(avoid),
    )
    params.update(kw)
    return run(generate_batch(gen, **params))


def make_settings(**kw) -> PaperSettings:
    base = {
        "track": "it",
        "counts": {"easy": 4, "medium": 4, "advanced": 2},
        "mix": {"aptitude": 40, "reasoning": 30, "technical": 30},
        "pool_factor": 2,
    }
    base.update(kw)
    return PaperSettings(**base)


async def _noop_progress(done: int, total: int) -> None:
    pass


# ---------------------------------------------------------------- batch validation


def test_valid_json_accepted_in_one_call_and_left_for_hr_to_verify():
    gen = ScriptedGen({"questions": [mcq(), mcq(answer=0), mcq(answer=3)]})
    res = batch(gen, count=3)
    assert res.rejected == 0
    assert len(res.accepted) == 3
    q = res.accepted[0]
    # no AI checks the key any more: HR does, so the question is not marked verified
    assert (q.level, q.section, q.style, q.verified) == ("easy", "aptitude", "aptitude", False)
    assert len({q.id for q in res.accepted}) == 3
    assert len(gen.calls) == 1  # free-tier budget: one provider call per batch


def test_malformed_json_rejected():
    gen = ScriptedGen({"questions": "not a list"}, agents.LlmOutputError("bad json"), {"oops": []})
    for _ in range(3):
        res = batch(gen, count=2)
        assert res.accepted == [] and res.rejected == 2
    assert len(gen.calls) == 3  # bad output is never retried inside a batch
    with pytest.raises(agents.LlmOutputError):
        parse_json_object("Sure! here is your JSON: {")
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_wrong_option_count_rejected():
    bad = mcq(options=["a", "b", "c"])
    gen = ScriptedGen({"questions": [bad, mcq(answer=7), mcq(answer=True), mcq()]})
    res = batch(gen, count=4)
    assert len(res.accepted) == 1 and res.rejected == 3


def test_duplicate_options_rejected():
    dup = mcq(options=["10", "20", " 10 ", "30"])
    aota = mcq(options=["1", "2", "3", "All of the above"])
    blank_why = mcq(why="  ")
    gen = ScriptedGen({"questions": [dup, aota, blank_why]})
    res = batch(gen, count=3)
    assert res.accepted == [] and res.rejected == 3


def test_role_reaches_only_the_technical_prompt():
    gen = ScriptedGen()
    settings = make_settings(role="  Java   Backend Developer ")
    run(generate_pool(gen, settings, on_progress=_noop_progress))
    line = "Role being hired: Java Backend Developer."
    assert all((line in u) == ("Section: technical" in u) for _, u in gen.calls)

    gen2 = ScriptedGen()  # drafts saved before `role` existed
    run(generate_pool(gen2, make_settings(), on_progress=_noop_progress))
    assert not any("Role being hired" in u for _, u in gen2.calls)


def test_duplicate_text_dropped():
    gen = ScriptedGen(
        {"questions": [mcq(text="Old  question?"), mcq(text="New one?"), mcq(text="new ONE?")]}
    )
    res = batch(gen, count=3, avoid=["old question?"])
    assert [q.text for q in res.accepted] == ["New one?"]
    assert res.rejected == 2
    assert "old question?" in gen.calls[0][1]  # the generator is told what to avoid


# ---------------------------------------------------------------- HTTP clients


@pytest.fixture
def settings_env(monkeypatch):
    def apply(**env):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


@pytest.fixture
def no_throttle(monkeypatch):
    """Retry tests assert the exact waits, so the request-slot wait is switched off."""

    async def free(self) -> None:
        pass

    monkeypatch.setattr(NvidiaClient, "_throttle", free)


def test_missing_key_raises_not_configured(settings_env):
    settings_env(NVIDIA_API_KEY="   ")  # explicit: else a real .env key leaks in
    assert providers_status() == {"nvidia": False}  # blank counts as missing
    with pytest.raises(AppError) as e:
        NvidiaClient()
    assert (e.value.status, e.value.code) == (503, "AI_PROVIDER_NOT_CONFIGURED")
    assert e.value.details == {"provider": "nvidia"}

    settings_env(NVIDIA_API_KEY="")
    assert providers_status() == {"nvidia": False}
    settings_env(NVIDIA_API_KEY=SECRET)
    assert providers_status() == {"nvidia": True}


def _sleeps():
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    return waits, sleep


def _ok(content: str = "{}") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_429_retries_patiently_then_rate_limited_error(no_throttle):
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(429, json={"error": "rate limited"})

    waits, sleep = _sleeps()
    client = NvidiaClient(SECRET, "m", sleep=sleep, transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as e:
        run(client.complete_json(system="s", user="u"))
    assert (e.value.status, e.value.code) == (429, "AI_PROVIDER_RATE_LIMITED")
    assert e.value.details == {"provider": "nvidia", "provider_status": 429}
    assert "Wait a minute" in e.value.message
    # rate limits get more attempts and longer waits than 5xx (1 minute provider windows)
    assert len(hits) == 6 and len(waits) == 5
    assert waits == sorted(waits) and waits[0] >= 5 and max(waits) <= 60


def test_retry_after_header_wins_and_is_capped(no_throttle):
    replies = [
        httpx.Response(429, headers={"retry-after": "500"}),
        httpx.Response(429, headers={"retry-after": "2"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return replies.pop(0) if replies else _ok()

    waits, sleep = _sleeps()
    client = NvidiaClient(SECRET, "m", sleep=sleep, transport=httpx.MockTransport(handler))
    assert run(client.complete_json(system="s", user="u")) == {}
    assert waits == [60.0, 2.0]


def test_5xx_keeps_three_short_attempts(no_throttle):
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(503)

    waits, sleep = _sleeps()
    client = NvidiaClient(SECRET, "m", sleep=sleep, transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as e:
        run(client.complete_json(system="s", user="u"))
    assert e.value.code == "AI_PROVIDER_ERROR" and e.value.details["provider_status"] == 503
    assert len(hits) == 3 and waits == [1.0, 2.0]


def test_retry_then_success_and_request_format(no_throttle):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # a fenced reply: models without a JSON mode often wrap their JSON
        return httpx.Response(503) if len(seen) == 1 else _ok('```json\n{"answers": []}\n```')

    waits, sleep = _sleeps()
    client = NvidiaClient(
        SECRET, "big-m", temperature=0.7, sleep=sleep, transport=httpx.MockTransport(handler)
    )
    assert run(client.complete_json(system="s", user="u")) == {"answers": []}
    req = seen[-1]
    assert str(req.url) == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {SECRET}"
    body = json.loads(req.content)
    assert body["model"] == "big-m" and body["temperature"] == 0.7
    assert body["max_tokens"] >= 4096  # room for a batch of 8 questions with explanations
    # without this, reasoning models return content=None after using every token on thinking
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert len(waits) == 1


def test_unusable_reply_is_an_output_error(no_throttle):
    replies = [httpx.Response(200, json={"choices": []}), _ok("Sure! Here you go")]
    _, sleep = _sleeps()
    transport = httpx.MockTransport(lambda request: replies.pop(0))
    client = NvidiaClient(SECRET, "m", sleep=sleep, transport=transport)
    for _ in range(2):
        with pytest.raises(agents.LlmOutputError):
            run(client.complete_json(system="s", user="u"))


def test_requests_are_spaced_to_stay_under_the_free_tier_limit(monkeypatch):
    monkeypatch.setattr(agents, "_next_slot", 0.0)
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(503) if len(hits) == 3 else _ok()

    waits, sleep = _sleeps()
    kw = dict(rpm=30, sleep=sleep, transport=httpx.MockTransport(handler))
    gen, other = NvidiaClient(SECRET, "gen-m", **kw), NvidiaClient(SECRET, "other-m", **kw)

    async def paper():
        await gen.complete_json(system="s", user="u")
        await other.complete_json(system="s", user="u")  # other client, same key: same clock
        await gen.complete_json(system="s", user="u")  # 503, then the retry also takes a slot

    run(paper())
    assert len(hits) == 4
    slots = [w for w in waits if w != 1.0]  # 1.0 is the 5xx retry backoff
    # the fake sleep does not move the clock, so the waits grow by one 2 s slot per request
    assert [round(w) for w in slots] == [2, 4, 6]


def test_keys_never_in_errors_or_logs(caplog, no_throttle):
    caplog.set_level(logging.DEBUG)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {request.headers['authorization']}", request=request)

    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"bad key {SECRET}"}})

    _, sleep = _sleeps()
    errors = []
    for handler in (boom, denied):
        client = NvidiaClient(SECRET, "m", sleep=sleep, transport=httpx.MockTransport(handler))
        assert SECRET not in repr(client)
        with pytest.raises(AppError) as e:
            run(client.complete_json(system="s", user="u"))
        errors.append(e.value)
    assert [e.code for e in errors] == ["AI_PROVIDER_ERROR", "AI_PROVIDER_ERROR"]
    for e in errors:
        assert SECRET not in f"{e.message} {e.details} {e.__cause__!r}"
    assert SECRET not in caplog.text
    assert caplog.records  # failures were logged, just without the key


# ---------------------------------------------------------------- pool


def _cell_counts(pool):
    out = {(lv, s): 0 for lv in LEVELS for s in SECTIONS}
    for q in pool:
        out[(q.level, q.section)] += 1
    return out


def test_generate_pool_covers_needed_counts_times_factor():
    settings = make_settings(counts={"easy": 10, "medium": 6, "advanced": 4}, pool_factor=2)
    gen = ScriptedGen()
    pool = run(generate_pool(gen, settings, on_progress=_noop_progress))
    want = needed(settings)
    counts = _cell_counts(pool)
    for lv in LEVELS:
        for s in SECTIONS:
            assert counts[(lv, s)] == want[lv][s] * 2
    assert len({q.text for q in pool}) == len(pool)
    # every generator call stays within a batch of 8
    assert all(int(re.search(r"Write exactly (\d+)", u).group(1)) <= 8 for _, u in gen.calls)
    tech = [q for q in pool if q.section == "technical"]
    assert {q.style for q in tech} == {"syntax", "concept"}


def test_generate_pool_raises_pool_too_small():
    gen = ScriptedGen(*[{"questions": []}] * 100)  # the model never returns a usable question
    with pytest.raises(AppError) as e:
        run(generate_pool(gen, make_settings(), on_progress=_noop_progress))
    assert (e.value.status, e.value.code) == (422, "POOL_TOO_SMALL")
    assert e.value.details["missing"]


def test_generate_pool_provider_error_propagates():
    class Down:
        async def complete_json(self, *, system, user):
            raise AppError(502, "AI_PROVIDER_ERROR", "down")

    with pytest.raises(AppError) as e:
        run(generate_pool(Down(), make_settings(), on_progress=_noop_progress))
    assert e.value.code == "AI_PROVIDER_ERROR"


def test_progress_reported_monotonically():
    events: list[tuple[int, int]] = []

    async def on_progress(done, total):
        events.append((done, total))

    gen = ScriptedGen()
    run(generate_pool(gen, make_settings(), on_progress=on_progress))
    assert events[0][0] == 0
    dones = [d for d, _ in events]
    assert dones == sorted(dones)
    assert all(d <= t for d, t in events)
    assert events[-1][0] == events[-1][1] > 0


def test_progress_never_goes_backwards_when_top_up_runs(monkeypatch):
    events: list[tuple[int, int]] = []
    top_ups: list[int] = []
    real_cell_plans = agents._cell_plans

    def spy(settings, level, section, count, topic_offset=0):
        if topic_offset:
            top_ups.append(count)
        return real_cell_plans(settings, level, section, count, topic_offset)

    monkeypatch.setattr(agents, "_cell_plans", spy)

    async def on_progress(done, total):
        events.append((done, total))

    gen = ScriptedGen(*[{"questions": []}] * 6)  # first 6 batches come back empty -> top-up
    settings = make_settings(pool_factor=1)
    pool = run(generate_pool(gen, settings, on_progress=on_progress))
    want = needed(settings)
    assert _cell_counts(pool) == {(lv, s): want[lv][s] for lv in LEVELS for s in SECTIONS}
    assert top_ups  # the scenario really exercised the top-up round

    fractions = [d / t for d, t in events]
    assert fractions == sorted(fractions), events
    assert [d for d, _ in events] == sorted(d for d, _ in events)
    assert len({t for _, t in events}) == 1  # total fixed from the first call
    assert events[-1][0] == events[-1][1] > 0


def test_generate_pool_runs_at_most_three_batches_at_once():
    active = peak = 0

    class SlowGen(ScriptedGen):
        async def complete_json(self, *, system, user):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            try:
                return await super().complete_json(system=system, user=user)
            finally:
                active -= 1

    gen = SlowGen()
    settings = make_settings(counts={"easy": 20, "medium": 20, "advanced": 20})
    run(generate_pool(gen, settings, on_progress=_noop_progress))
    assert 1 < peak <= 3


def test_it_prompt_mentions_syntax_and_non_it_mentions_soft_skills():
    gen = ScriptedGen()
    run(generate_pool(gen, make_settings(), on_progress=_noop_progress))
    it_tech = [u for _, u in gen.calls if "Section: technical" in u]
    assert it_tech and any("SYNTAX" in u and "fenced code block" in u for u in it_tech)
    assert any("CONCEPT" in u for u in it_tech)
    assert not any("SOFT SKILLS" in u for u in it_tech)

    gen2 = ScriptedGen()
    settings = make_settings(track="non_it")
    run(generate_pool(gen2, settings, on_progress=_noop_progress))
    non_it = [u for _, u in gen2.calls if "Section: technical" in u]
    assert non_it and all("SOFT SKILLS" in u for u in non_it)
    assert not any("SYNTAX" in u for u in non_it)

    apt = [s + u for s, u in gen.calls if "Section: aptitude" in u]
    assert apt and all("worked solution" in x for x in apt)
    users = [u for _, u in gen.calls]
    assert any("easy: solvable in one step" in u for u in users)
    assert any("medium: needs two steps" in u for u in users)
    assert any("advanced: multi-step" in u for u in users)


def test_prompt_overrides():
    default = agents.DEFAULT_PROMPTS["paper_compose"]
    assert get_prompt("paper_compose") == default
    assert get_prompt("paper_compose", {"paper_compose": "CUSTOM"}) == "CUSTOM"
    assert get_prompt("paper_compose", {"paper_compose": "  "}) == default
    with pytest.raises(ValueError):
        get_prompt("nope")

    gen = ScriptedGen({"questions": [mcq()]})
    batch(gen, count=1, overrides={"paper_gen_aptitude": "GEN-X"})
    assert gen.calls[0][0] == "GEN-X"

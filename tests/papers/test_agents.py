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
    GeminiClient,
    GroqClient,
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


class OracleVerifier:
    """Answers correctly using the generator's key (looked up by text), unless told to disagree."""

    def __init__(self, gen: ScriptedGen, wrong_on: set[str] = frozenset()):
        self.gen, self.wrong_on = gen, wrong_on
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, *, system: str, user: str) -> dict:
        self.calls.append((system, user))
        text = " ".join(user.split("Question:\n", 1)[1].split("\n\nOptions")[0].split())
        answer = self.gen.answers[text]
        if any(w in text for w in self.wrong_on):
            answer = (answer + 1) % 4
        return {"work": "solved", "answer": answer}


def run(coro):
    return asyncio.run(coro)


def batch(gen, ver, count=3, avoid=(), **kw):
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
    return run(generate_batch(gen, ver, **params))


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


def test_valid_json_accepted():
    gen = ScriptedGen({"questions": [mcq(), mcq(answer=0), mcq(answer=3)]})
    ver = OracleVerifier(gen)
    res = batch(gen, ver, count=3)
    assert res.rejected == 0
    assert len(res.accepted) == 3
    q = res.accepted[0]
    assert (q.level, q.section, q.style, q.verified) == ("easy", "aptitude", "aptitude", True)
    assert len({q.id for q in res.accepted}) == 3


def test_malformed_json_rejected():
    gen = ScriptedGen({"questions": "not a list"}, agents.LlmOutputError("bad json"), {"oops": []})
    ver = OracleVerifier(gen)
    for _ in range(3):
        res = batch(gen, ver, count=2)
        assert res.accepted == [] and res.rejected == 2
    assert ver.calls == []
    with pytest.raises(agents.LlmOutputError):
        parse_json_object("Sure! here is your JSON: {")
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_wrong_option_count_rejected():
    bad = mcq(options=["a", "b", "c"])
    gen = ScriptedGen({"questions": [bad, mcq(answer=7), mcq(answer=True), mcq()]})
    res = batch(gen, OracleVerifier(gen), count=4)
    assert len(res.accepted) == 1 and res.rejected == 3


def test_duplicate_options_rejected():
    dup = mcq(options=["10", "20", " 10 ", "30"])
    aota = mcq(options=["1", "2", "3", "All of the above"])
    blank_why = mcq(why="  ")
    gen = ScriptedGen({"questions": [dup, aota, blank_why]})
    res = batch(gen, OracleVerifier(gen), count=3)
    assert res.accepted == [] and res.rejected == 3


def test_verifier_disagreement_rejected_after_one_retry():
    first, regen = mcq(text="Disputed one?"), mcq(text="Disputed again?")
    gen = ScriptedGen({"questions": [first, mcq(text="Fine question?")]}, {"questions": [regen]})
    ver = OracleVerifier(gen, wrong_on={"Disputed"})
    res = batch(gen, ver, count=2)
    assert [q.text for q in res.accepted] == ["Fine question?"]
    assert res.rejected == 1
    assert len(gen.calls) == 2  # exactly one regeneration
    assert "Write exactly 1" in gen.calls[1][1]

    # a regenerated question that the verifier agrees with is kept
    gen2 = ScriptedGen({"questions": [mcq(text="Disputed x?")]}, {"questions": [mcq()]})
    res2 = batch(gen2, OracleVerifier(gen2, wrong_on={"Disputed"}), count=1)
    assert len(res2.accepted) == 1 and res2.rejected == 0


def test_verifier_is_blind():
    q = mcq(text="What is 10% of 50?", why="SECRET-WHY 10/100*50 = 5", answer=2)
    gen = ScriptedGen({"questions": [q]})
    ver = OracleVerifier(gen)
    batch(gen, ver, count=1)
    ((system, user),) = ver.calls
    assert "SECRET-WHY" not in system + user
    assert "What is 10% of 50?" in user and all(o in user for o in q["options"])
    assert '"answer": 2' not in user and "correct" not in user.lower()


def test_unusable_verifier_reply_counts_as_disagreement():
    gen = ScriptedGen({"questions": [mcq()]}, {"questions": [mcq()]})

    class Garbage:
        async def complete_json(self, *, system, user):
            return {"answer": "B?"}

    res = batch(gen, Garbage(), count=1)
    assert res.accepted == [] and res.rejected == 1


def test_duplicate_text_dropped():
    gen = ScriptedGen(
        {"questions": [mcq(text="Old  question?"), mcq(text="New one?"), mcq(text="new ONE?")]}
    )
    res = batch(gen, OracleVerifier(gen), count=3, avoid=["old question?"])
    assert [q.text for q in res.accepted] == ["New one?"]
    assert res.rejected == 2
    assert "old question?" in gen.calls[0][1]  # the generator is told what to avoid


# ---------------------------------------------------------------- HTTP clients


@pytest.fixture
def settings_env(monkeypatch):
    def apply(**env):
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


def test_missing_key_raises_not_configured(settings_env):
    settings_env(GEMINI_API_KEY="", GROQ_API_KEY="   ")
    assert providers_status() == {"gemini": False, "groq": False}
    for cls in (GeminiClient, GroqClient):
        with pytest.raises(AppError) as e:
            cls()
        assert (e.value.status, e.value.code) == (503, "AI_PROVIDER_NOT_CONFIGURED")

    settings_env(GEMINI_API_KEY=SECRET, GROQ_API_KEY="")  # explicit: else a real .env key leaks in
    assert providers_status() == {"gemini": True, "groq": False}


def _sleeps():
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    return waits, sleep


def _google_429(*details):
    return httpx.Response(429, json={"error": {"code": 429, "details": list(details)}})


RETRY_INFO = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}


def test_429_retries_patiently_then_rate_limited_error():
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(429, json={"error": "rate limited"})

    waits, sleep = _sleeps()
    client = GroqClient(SECRET, "m", sleep=sleep, transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as e:
        run(client.complete_json(system="s", user="u"))
    assert (e.value.status, e.value.code) == (429, "AI_PROVIDER_RATE_LIMITED")
    assert e.value.details == {"provider": "groq", "provider_status": 429}
    assert "Wait a minute" in e.value.message
    # rate limits get more attempts and longer waits than 5xx (1 minute provider windows)
    assert len(hits) == 6 and len(waits) == 5
    assert waits == sorted(waits) and waits[0] >= 5 and max(waits) <= 60


def test_429_waits_for_gemini_retry_delay_in_body():
    replies = [_google_429(RETRY_INFO)]

    def handler(request: httpx.Request) -> httpx.Response:
        if replies:
            return replies.pop()
        parts = [{"text": '{"questions": []}'}]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": parts}}]})

    waits, sleep = _sleeps()
    gem = GeminiClient(SECRET, "m", sleep=sleep, transport=httpx.MockTransport(handler))
    assert run(gem.complete_json(system="s", user="u")) == {"questions": []}
    assert waits == [37.0]


def test_retry_after_header_wins_and_is_capped():
    replies = [
        httpx.Response(429, headers={"retry-after": "500"}),
        httpx.Response(429, headers={"retry-after": "2"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if replies:
            return replies.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    waits, sleep = _sleeps()
    groq = GroqClient(SECRET, "m", sleep=sleep, transport=httpx.MockTransport(handler))
    assert run(groq.complete_json(system="s", user="u")) == {}
    assert waits == [60.0, 2.0]


def test_429_daily_quota_fails_fast_with_clear_message():
    hits = []
    quota = {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return _google_429(quota, RETRY_INFO)

    waits, sleep = _sleeps()
    gem = GeminiClient(SECRET, "m", sleep=sleep, transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as e:
        run(gem.complete_json(system="s", user="u"))
    assert (e.value.status, e.value.code) == (429, "AI_PROVIDER_QUOTA_EXHAUSTED")
    assert "daily" in e.value.message and e.value.details["provider"] == "gemini"
    assert len(hits) == 1 and waits == []


def test_5xx_keeps_three_short_attempts():
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(503)

    waits, sleep = _sleeps()
    groq = GroqClient(SECRET, "m", sleep=sleep, transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as e:
        run(groq.complete_json(system="s", user="u"))
    assert e.value.code == "AI_PROVIDER_ERROR" and e.value.details["provider_status"] == 503
    assert len(hits) == 3 and waits == [1.0, 2.0]


def test_retry_then_success_and_request_formats():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(503)
        if "groq" in request.url.host:
            content = '{"answer": 2}'
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        parts = [{"text": "thinking...", "thought": True}, {"text": '{"questions": []}'}]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": parts}}]})

    waits, sleep = _sleeps()
    groq = GroqClient(SECRET, "groq-m", sleep=sleep, transport=httpx.MockTransport(handler))
    assert run(groq.complete_json(system="s", user="u")) == {"answer": 2}
    body = json.loads(seen[-1].content)
    assert body["response_format"] == {"type": "json_object"} and body["model"] == "groq-m"
    assert seen[-1].headers["authorization"] == f"Bearer {SECRET}"
    assert len(waits) == 1

    seen.clear()
    gem = GeminiClient(SECRET, "gem-m", sleep=sleep, transport=httpx.MockTransport(handler))
    assert run(gem.complete_json(system="s", user="u")) == {"questions": []}
    req = seen[-1]
    assert req.url.path.endswith("/models/gem-m:generateContent")
    assert SECRET not in str(req.url)
    assert req.headers["x-goog-api-key"] == SECRET
    body = json.loads(req.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_keys_never_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("groq.com"):
            raise httpx.ConnectError(f"boom {request.headers['authorization']}", request=request)
        return httpx.Response(401, json={"error": {"message": f"bad key {SECRET}"}})

    _, sleep = _sleeps()
    errors = []
    for cls in (GeminiClient, GroqClient):
        client = cls(SECRET, "m", sleep=sleep, transport=httpx.MockTransport(handler))
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
    pool = run(generate_pool(gen, OracleVerifier(gen), settings, on_progress=_noop_progress))
    want = needed(settings)
    counts = _cell_counts(pool)
    for lv in LEVELS:
        for s in SECTIONS:
            assert counts[(lv, s)] == want[lv][s] * 2
    assert len({q.text for q in pool}) == len(pool)
    assert all(q.verified for q in pool)
    # every generator call stays within a batch of 8
    assert all(int(re.search(r"Write exactly (\d+)", u).group(1)) <= 8 for _, u in gen.calls)
    tech = [q for q in pool if q.section == "technical"]
    assert {q.style for q in tech} == {"syntax", "concept"}


def test_generate_pool_raises_pool_too_small():
    settings = make_settings()
    gen = ScriptedGen()
    ver = OracleVerifier(gen, wrong_on={"Question"})  # verifier rejects everything
    with pytest.raises(AppError) as e:
        run(generate_pool(gen, ver, settings, on_progress=_noop_progress))
    assert (e.value.status, e.value.code) == (422, "POOL_TOO_SMALL")
    assert e.value.details["missing"]


def test_generate_pool_provider_error_propagates():
    class Down:
        async def complete_json(self, *, system, user):
            raise AppError(502, "AI_PROVIDER_ERROR", "down")

    gen = ScriptedGen()
    with pytest.raises(AppError) as e:
        run(generate_pool(gen, Down(), make_settings(), on_progress=_noop_progress))
    assert e.value.code == "AI_PROVIDER_ERROR"


def test_progress_reported_monotonically():
    events: list[tuple[int, int]] = []

    async def on_progress(done, total):
        events.append((done, total))

    gen = ScriptedGen()
    run(generate_pool(gen, OracleVerifier(gen), make_settings(), on_progress=on_progress))
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

    class RejectFirst(OracleVerifier):
        """Disagrees on the first 6 verifications, so cells fall short and a top-up runs."""

        async def complete_json(self, *, system, user):
            reply = await super().complete_json(system=system, user=user)
            if len(self.calls) <= 6:
                reply["answer"] = (reply["answer"] + 1) % 4
            return reply

    gen = ScriptedGen()
    settings = make_settings(pool_factor=1)
    pool = run(generate_pool(gen, RejectFirst(gen), settings, on_progress=on_progress))
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
    run(generate_pool(gen, OracleVerifier(gen), settings, on_progress=_noop_progress))
    assert 1 < peak <= 3


def test_it_prompt_mentions_syntax_and_non_it_mentions_soft_skills():
    gen = ScriptedGen()
    run(generate_pool(gen, OracleVerifier(gen), make_settings(), on_progress=_noop_progress))
    it_tech = [u for _, u in gen.calls if "Section: technical" in u]
    assert it_tech and any("SYNTAX" in u and "fenced code block" in u for u in it_tech)
    assert any("CONCEPT" in u for u in it_tech)
    assert not any("SOFT SKILLS" in u for u in it_tech)

    gen2 = ScriptedGen()
    settings = make_settings(track="non_it")
    run(generate_pool(gen2, OracleVerifier(gen2), settings, on_progress=_noop_progress))
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
    assert get_prompt("paper_verify") == agents.DEFAULT_PROMPTS["paper_verify"]
    assert get_prompt("paper_verify", {"paper_verify": "CUSTOM"}) == "CUSTOM"
    assert (
        get_prompt("paper_verify", {"paper_verify": "  "}) == agents.DEFAULT_PROMPTS["paper_verify"]
    )
    with pytest.raises(ValueError):
        get_prompt("nope")

    gen = ScriptedGen({"questions": [mcq()]})
    ver = OracleVerifier(gen)
    batch(gen, ver, count=1, overrides={"paper_gen_aptitude": "GEN-X", "paper_verify": "VER-X"})
    assert gen.calls[0][0] == "GEN-X" and ver.calls[0][0] == "VER-X"

"""Round 3 AI interview: token minting, prompts, score schema and the in-memory frame store."""

import asyncio
import functools
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import get_settings
from app.core.errors import AppError
from app.interviews import frames, gemini, prompts, scoring
from app.interviews.schemas import GeneratedScores
from app.papers.agents import LlmOutputError

KEY = "gem-secret-key-123"


def sync(fn):
    """No async pytest plugin here: run the coroutine test to completion."""

    @functools.wraps(fn)
    def run(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return run


NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(get_settings(), "gemini_api_key", KEY)
    monkeypatch.setattr(get_settings(), "gemini_live_model", "gemini-3.8-live")
    monkeypatch.setattr(get_settings(), "gemini_live_voice", "Kore")


def _mint(handler):
    return gemini.mint_token(
        system_instruction="BE AN INTERVIEWER",
        expire_at=NOW + timedelta(minutes=45),
        new_session_until=NOW + timedelta(minutes=2),
        transport=httpx.MockTransport(handler),
    )


# ------------------------------------------------------------------------------------ gemini


@sync
async def test_token_request_locks_config_and_keeps_key_in_header(key):
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"name": "auth_tokens/abc"})

    token, model = await _mint(handler)
    assert (token, model) == ("auth_tokens/abc", "gemini-3.8-live")
    assert seen["headers"]["x-goog-api-key"] == KEY
    assert KEY not in seen["url"] and KEY not in json.dumps(seen["body"])
    body = seen["body"]
    assert body["uses"] == 1
    assert body["expireTime"] == "2026-11-13T10:45:00Z"
    assert body["newSessionExpireTime"] == "2026-11-13T10:02:00Z"
    c = body["liveConnectConstraints"]
    assert c["model"] == "models/gemini-3.8-live"
    cfg = c["config"]
    assert cfg["systemInstruction"] == {"parts": [{"text": "BE AN INTERVIEWER"}]}
    assert cfg["responseModalities"] == ["AUDIO"]
    assert cfg["inputAudioTranscription"] == {} and cfg["outputAudioTranscription"] == {}
    assert cfg["contextWindowCompression"] == {"slidingWindow": {}}
    assert cfg["sessionResumption"] == {}
    assert cfg["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"


@sync
async def test_missing_key_is_not_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "gemini_api_key", None)
    with pytest.raises(AppError) as e:
        await _mint(lambda r: httpx.Response(200, json={"name": "x"}))
    assert (e.value.status, e.value.code) == (503, "AI_NOT_CONFIGURED")


@pytest.mark.parametrize(
    ("status", "code"), [(429, "AI_PROVIDER_RATE_LIMITED"), (500, "AI_PROVIDER_ERROR")]
)
@sync
async def test_provider_errors_never_carry_the_key(key, status, code):
    with pytest.raises(AppError) as e:
        await _mint(lambda r: httpx.Response(status, text=f"echo {KEY}"))
    assert e.value.code == code
    assert KEY not in e.value.message and KEY not in json.dumps(e.value.details)


@sync
async def test_reply_without_token_is_a_provider_error(key):
    with pytest.raises(AppError) as e:
        await _mint(lambda r: httpx.Response(200, json={"nope": 1}))
    assert e.value.code == "AI_PROVIDER_ERROR"


@sync
async def test_network_failure_is_a_provider_error(key):
    def boom(_):
        raise httpx.ConnectError("down")

    with pytest.raises(AppError) as e:
        await _mint(boom)
    assert e.value.status == 502


# ----------------------------------------------------------------------------------- prompts


@pytest.mark.parametrize("minutes", [1, 7, 20, 35, 40, 45, 60])
def test_section_plan_adds_up(minutes):
    plan = prompts.section_plan(minutes)
    assert sum(m for _, m, _ in plan) == minutes
    assert [s for s, _, _ in plan][0] == "Introduction" and plan[-1][0] == "Closing"


def test_system_instruction_fills_every_placeholder_and_tolerates_braces():
    text = prompts.build_system_instruction(
        company="Ampera",
        role="Backend Engineer",
        name="Asha",
        minutes=40,
        skills=["Python", "SQL"],
        jd="Build APIs {with} care {role}",
    )
    assert "Ampera" in text and "Backend Engineer" in text and "Asha" in text
    assert "40 minutes" in text and "Python, SQL" in text
    assert "{with}" in text  # job description braces are left alone
    assert prompts.CLOSING_PHRASE in text and prompts.SYSTEM_TAG in text
    for marker in ("{company}", "{name}", "{minutes}", "{plan}", "{skills}", "{resume_note}"):
        assert marker not in text
    assert "continuation" not in text


def test_system_instruction_resumes_without_greeting_again():
    turns = [{"role": "ai", "text": "Tell me about X"}, {"role": "candidate", "text": "I built Y"}]
    text = prompts.build_system_instruction(
        company="A", role="R", name="N", minutes=12, skills=[], jd="", earlier_turns=turns
    )
    assert "Do NOT greet again" in text and "Candidate: I built Y" in text


def test_override_replaces_default():
    text = prompts.build_system_instruction(
        company="A",
        role="R",
        name="N",
        minutes=5,
        skills=[],
        jd="",
        overrides={"interview_system": "Custom for {name}"},
    )
    assert text == "Custom for N"


def test_long_transcript_keeps_start_and_end():
    turns = [{"role": "candidate", "text": f"turn {i} " + "x" * 100} for i in range(2000)]
    out = prompts.format_transcript(turns, max_chars=10_000)
    assert len(out) < 10_200 and "turn 0 " in out and "turn 1999 " in out and "omitted" in out


# ------------------------------------------------------------------------------------ scores

VALID = {
    "communication": 4,
    "technical_depth": 3,
    "problem_solving": 3,
    "culture": 5,
    "sentiment": {"overall": "positive", "confidence": "medium", "note": "Upbeat."},
    "summary": "Clear answers.",
}


def test_score_schema_accepts_valid():
    assert GeneratedScores.model_validate(VALID).culture == 5


@pytest.mark.parametrize(
    "patch",
    [
        {"communication": 0},
        {"culture": 6},
        {"technical_depth": 3.5},
        {"problem_solving": True},
        {"communication": "4"},
        {"sentiment": {"overall": "great", "confidence": "low", "note": ""}},
        {"sentiment": {"overall": "neutral", "confidence": "low", "note": "x" * 301}},
        {"summary": "x" * 1201},
        {"summary": ""},
    ],
)
def test_score_schema_rejects_invalid(patch):
    with pytest.raises(ValueError):
        GeneratedScores.model_validate({**VALID, **patch})


class FakeLlm:
    model = "fake-model"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def complete_json(self, *, system, user):
        self.calls.append((system, user))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


TURNS = [{"role": "ai", "text": "Hi"}, {"role": "candidate", "text": "Hello, I'm Asha"}]


@sync
async def test_scoring_retries_once_then_succeeds():
    llm = FakeLlm([{"bad": 1}, VALID])
    out = await scoring.score_transcript(llm, "fake-model", TURNS)
    assert out.communication == 4 and out.model == "fake-model" and len(llm.calls) == 2
    assert "CANDIDATE: Hello, I'm Asha" in llm.calls[0][1]


@sync
async def test_scoring_gives_up_after_two_invalid_replies():
    llm = FakeLlm([LlmOutputError("x"), {**VALID, "culture": 9}])
    with pytest.raises(scoring.ScoringFailed) as e:
        await scoring.score_transcript(llm, "m", TURNS)
    assert e.value.code == "INVALID_OUTPUT"


@sync
async def test_scoring_refuses_a_transcript_with_no_candidate_turns():
    with pytest.raises(scoring.ScoringFailed) as e:
        await scoring.score_transcript(FakeLlm([]), "m", [{"role": "ai", "text": "Hi"}])
    assert e.value.code == "EMPTY_TRANSCRIPT"


# ------------------------------------------------------------------------------------ frames

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100


def test_frames_expire_and_check_magic():
    frames.clear()
    iid = __import__("uuid").uuid4()
    frames.put(iid, JPEG, now=100.0)
    assert frames.get(iid, now=105.0) == JPEG
    assert frames.get(iid, now=100.0 + frames.FRESH_SECONDS + 1) is None
    assert frames.is_jpeg(JPEG) and not frames.is_jpeg(b"\x89PNG....")
    frames.drop(iid)
    assert frames.get(iid, now=100.0) is None


# ------------------------------------------------------------------------- typed answers


@sync
async def test_token_config_declares_the_answer_box_tool(key):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"name": "t"})

    await _mint(handler)
    tools = seen["body"]["liveConnectConstraints"]["config"]["tools"]
    decl = tools[0]["functionDeclarations"][0]
    assert decl["name"] == "show_answer_box"
    assert decl["parameters"]["required"] == ["question", "mode"]


def test_prompt_explains_the_answer_box_and_adds_reference_only_when_given():
    base = dict(company="A", role="R", name="N", minutes=40, skills=[], jd="")
    plain = prompts.build_system_instruction(**base)
    assert "show_answer_box" in plain and prompts.TYPED_PREFIX in plain
    assert "Reference material" not in plain
    withref = prompts.build_system_instruction(**base, reference="Normalization: 1NF, 2NF")
    assert "Reference material" in withref and "1NF, 2NF" in withref


def test_transcript_marks_typed_answers_for_the_scorer():
    out = prompts.format_transcript(
        [
            {"role": "ai", "kind": "question", "text": "Reverse a list"},
            {"role": "candidate", "kind": "typed", "language": "python", "text": "x[::-1]"},
        ]
    )
    assert "written question shown on screen): Reverse a list" in out
    assert "CANDIDATE (typed answer, python):\n```\nx[::-1]\n```" in out

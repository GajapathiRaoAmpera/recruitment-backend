"""Composer (one Gemini call that picks which candidates are selected). Fake clients only."""

import asyncio
import json
import re
import uuid

import pytest

from app.core.errors import AppError
from app.papers import agents
from app.papers.agents import LlmOutputError, compose_selection
from app.papers.schemas import PoolQuestion


def q(level="easy", section="aptitude", topic="percentages", text=None, **kw) -> PoolQuestion:
    n = uuid.uuid4().hex[:8]
    base = dict(
        id=str(uuid.uuid4()),
        level=level,
        section=section,
        topic=topic,
        style=section if section != "technical" else "concept",
        text=text or f"Question {n}?",
        options=[f"OPTSECRET-{n}-{i}" for i in range(4)],
        answer=2,
        why=f"WHYSECRET-{n}",
    )
    return PoolQuestion(**(base | kw))


def cell(level, section, n, **kw) -> list[PoolQuestion]:
    return [q(level, section, **kw) for _ in range(n)]


def run(coro):
    return asyncio.run(coro)


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, *, system: str, user: str) -> dict:
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply(user) if callable(reply) else reply


def first_n(user: str) -> dict:
    """A well-behaved composer: keeps the first N candidates of each requested cell."""
    want = {m[0]: int(m[1]) for m in re.findall(r"^- (\w+/\w+): keep (\d+) of", user, re.M)}
    keep = []
    for line in user.splitlines():
        if line.startswith("{"):
            c = json.loads(line)
            key = f"{c['level']}/{c['section']}"
            if want.get(key, 0) > 0:
                keep.append(c["id"])
                want[key] -= 1
    return {"keep": keep}


def test_composer_prompt_contains_no_options_answers_or_why():
    long_text = "Long stem " + "x" * 400 + " TAILSECRET"
    cands = [*cell("easy", "aptitude", 3), q("easy", "aptitude", text=long_text)]
    client = Scripted(first_n)
    keep = run(compose_selection(client, cands, {("easy", "aptitude"): 2}))
    assert len(keep) == 2
    system, user = client.calls[0]
    sent = system + user
    assert "OPTSECRET" not in sent and "WHYSECRET" not in sent
    assert '"answer"' not in user and '"options"' not in user and '"why"' not in user
    assert "TAILSECRET" not in sent  # text truncated to 200 chars
    lines = [json.loads(line) for line in user.splitlines() if line.startswith("{")]
    assert len(lines) == 4
    assert all(set(c) == {"id", "level", "section", "topic", "text"} for c in lines)
    assert all(len(c["text"]) <= 200 for c in lines)


def test_composer_exact_counts_per_cell():
    cands = [
        *cell("easy", "aptitude", 6),
        *cell("easy", "technical", 5),
        *cell("medium", "reasoning", 4),
    ]
    target = {("easy", "aptitude"): 4, ("easy", "technical"): 3, ("medium", "reasoning"): 2}
    keep = run(compose_selection(Scripted(first_n), cands, target))
    by_cell: dict = {}
    for c in cands:
        if c.id in keep:
            by_cell[(c.level, c.section)] = by_cell.get((c.level, c.section), 0) + 1
    assert by_cell == target


def test_composer_invalid_ids_retry_then_error():
    cands = cell("easy", "aptitude", 4)
    bad = {"keep": ["not-a-candidate", cands[0].id]}
    client = Scripted(bad, bad)
    with pytest.raises(AppError) as e:
        run(compose_selection(client, cands, {("easy", "aptitude"): 2}))
    assert (e.value.status, e.value.code) == (502, "AI_COMPOSE_INVALID")
    assert len(client.calls) == 2
    assert "rejected" in client.calls[1][1]  # the retry says what was wrong


def test_composer_retry_succeeds_after_one_invalid_reply():
    cands = cell("easy", "aptitude", 4)
    client = Scripted(LlmOutputError("not json"), first_n)
    keep = run(compose_selection(client, cands, {("easy", "aptitude"): 2}))
    assert keep == {cands[0].id, cands[1].id} and len(client.calls) == 2


@pytest.mark.parametrize(
    "make_reply",
    [
        lambda c: {"keep": [c[0].id]},  # too few
        lambda c: {"keep": [c[0].id, c[1].id, c[2].id]},  # too many
        lambda c: {"keep": [c[0].id, c[0].id]},  # duplicate
        lambda c: {"keep": "everything"},  # wrong shape
        lambda c: {"ids": [c[0].id, c[1].id]},  # wrong key
        lambda c: {"keep": [c[0].id, 5]},  # non-string id
    ],
)
def test_composer_wrong_counts_rejected(make_reply):
    cands = cell("easy", "aptitude", 4)
    client = Scripted(make_reply(cands), make_reply(cands))
    with pytest.raises(AppError) as e:
        run(compose_selection(client, cands, {("easy", "aptitude"): 2}))
    assert e.value.code == "AI_COMPOSE_INVALID" and len(client.calls) == 2


def test_composer_rejects_ids_from_cells_not_requested():
    cands = [*cell("easy", "aptitude", 4), *cell("medium", "aptitude", 4)]
    # right total, but one id from a cell with target 1 and none from the other
    wrong = {"keep": [cands[0].id, cands[1].id]}
    client = Scripted(wrong, wrong)
    with pytest.raises(AppError):
        run(compose_selection(client, cands, {("easy", "aptitude"): 1, ("medium", "aptitude"): 1}))


def test_cells_kept_whole_skip_the_call():
    cands = cell("easy", "technical", 3)
    client = Scripted()
    keep = run(compose_selection(client, cands, {("easy", "technical"): 3}))
    assert keep == {c.id for c in cands} and client.calls == []


def test_only_cells_with_a_choice_are_sent():
    whole = cell("easy", "technical", 3)
    choose = cell("easy", "aptitude", 5)
    client = Scripted(first_n)
    keep = run(
        compose_selection(
            client, [*whole, *choose], {("easy", "technical"): 3, ("easy", "aptitude"): 2}
        )
    )
    assert {c.id for c in whole} <= keep and len(keep) == 5
    user = client.calls[0][1]
    assert all(c.id not in user for c in whole)


def test_target_larger_than_candidates_is_a_bug():
    with pytest.raises(ValueError):
        run(compose_selection(Scripted(), cell("easy", "aptitude", 2), {("easy", "aptitude"): 3}))


def test_provider_errors_propagate_without_retry():
    client = Scripted(AppError(502, "AI_PROVIDER_ERROR", "down"))
    with pytest.raises(AppError) as e:
        run(compose_selection(client, cell("easy", "aptitude", 4), {("easy", "aptitude"): 2}))
    assert e.value.code == "AI_PROVIDER_ERROR" and len(client.calls) == 1


def test_compose_prompt_override():
    client = Scripted(first_n)
    cands = cell("easy", "aptitude", 4)
    run(
        compose_selection(
            client, cands, {("easy", "aptitude"): 2}, overrides={"paper_compose": "CUSTOM BODY"}
        )
    )
    assert client.calls[0][0] == "CUSTOM BODY"
    assert agents.get_prompt("paper_compose") == agents.DEFAULT_PROMPTS["paper_compose"]

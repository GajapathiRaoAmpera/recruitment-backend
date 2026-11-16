"""Pool builder (code generators + LLM technical + composer). Fakes only: no DB, no network."""

import asyncio
import json
import uuid
from collections import Counter

import pytest

from app.core.config import get_settings
from app.core.errors import AppError
from app.papers import builder, pool, repo
from app.papers.builder import needed
from app.papers.schemas import LEVELS, PaperSettings, PoolQuestion
from tests.papers.test_agents import ScriptedGen
from tests.papers.test_compose import first_n
from tests.papers.test_repo_sql import RecordingSession

GENERATED = ("aptitude", "reasoning")  # code generators: one spare set per cell for HR's picker


def make_settings(**kw) -> PaperSettings:
    base = {
        "track": "it",
        "counts": {"easy": 4, "medium": 3, "advanced": 3},
        "mix": {"aptitude": 40, "reasoning": 30, "technical": 30},
        "pool_factor": 2,
    }
    return PaperSettings(**(base | kw))


class Composer:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, *, system, user):
        self.calls.append((system, user))
        reply = self.replies.pop(0) if self.replies else first_n
        return reply(user) if callable(reply) else reply


def run(coro):
    return asyncio.run(coro)


async def _noop(done, total):
    pass


def build(settings, *, composer=None, gen=None, events=None) -> list[PoolQuestion]:
    async def on_progress(done, total):
        if events is not None:
            events.append((done, total))

    gen = gen or ScriptedGen()
    return run(
        pool.build_pool(
            settings,
            on_progress=on_progress,
            overrides=None,
            draft_id=uuid.uuid4(),
            composer_client=composer or Composer(),
            generator_client=gen,
        )
    )


def cells(qs) -> Counter:
    return Counter((q.level, q.section) for q in qs)


# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("track", "styles"), [("it", {"syntax", "concept"}), ("non_it", {"soft_skill"})]
)
def test_pool_has_expected_cells_and_llm_writes_technical_only(track, styles):
    gen = ScriptedGen()
    settings = make_settings(track=track, role="Java Backend Developer")
    qs = build(settings, gen=gen)
    want = needed(settings)
    sel = [q for q in qs if q.selected]
    for lv in LEVELS:
        for s in GENERATED:
            assert cells(qs)[(lv, s)] == want[lv][s] * 3  # needed x (pool_factor + 1)
            assert cells(sel)[(lv, s)] == want[lv][s] * 2  # needed x pool_factor
        assert cells(qs)[(lv, "technical")] == cells(sel)[(lv, "technical")]
        assert cells(sel)[(lv, "technical")] == want[lv]["technical"] * 2
    # no question bank: every Technical question is written for the role, both tracks
    assert gen.calls and all("Section: technical" in u for _, u in gen.calls)
    assert all("Role being hired: Java Backend Developer." in u for _, u in gen.calls)
    tech = [q for q in qs if q.section == "technical"]
    assert {q.style for q in tech} == styles and all(q.source_id is None for q in tech)
    assert all(q.text.startswith("Question number") == (q.section == "technical") for q in qs)
    assert len({q.id for q in qs}) == len(qs)
    assert all(c.ok for c in builder.run_checks(settings, "d1", sel))


def test_unselected_candidates_remain_in_pool():
    settings = make_settings()
    unselected = [q for q in build(settings) if not q.selected]
    want = needed(settings)
    assert len(unselected) == sum(want[lv][s] for lv in LEVELS for s in GENERATED)
    assert {q.section for q in unselected} == set(GENERATED)


def test_composer_prompt_has_no_answers():
    composer = Composer()
    qs = build(make_settings(), composer=composer)
    assert len(composer.calls) == 1  # ONE composer call
    system, user = composer.calls[0]
    for q in qs:
        assert q.why not in user
        assert all(f'"{o}"' not in user for o in q.options)
    lines = [json.loads(x) for x in user.splitlines() if x.startswith("{")]
    assert all(set(c) == {"id", "level", "section", "topic", "text"} for c in lines)


def test_composer_invalid_reply_fails_the_pool():
    composer = Composer({"keep": []}, {"keep": ["x"]})
    with pytest.raises(AppError) as e:
        build(make_settings(), composer=composer)
    assert e.value.code == "AI_COMPOSE_INVALID" and len(composer.calls) == 2


@pytest.mark.parametrize("track", ["it", "non_it"])
def test_progress_monotonic(track):
    events: list[tuple[int, int]] = []
    build(make_settings(track=track), events=events)
    assert events[0][0] == 0
    assert [d for d, _ in events] == sorted(d for d, _ in events)
    assert len({t for _, t in events}) == 1 and events[0][1] > 0
    assert events[-1][0] == events[-1][1]
    assert len(events) >= 4  # generators -> technical -> compose -> done


@pytest.mark.parametrize("track", ["it", "non_it"])
def test_missing_nvidia_key_not_configured(monkeypatch, track):
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    get_settings.cache_clear()
    events: list[tuple[int, int]] = []

    async def on_progress(done, total):
        events.append((done, total))

    try:
        with pytest.raises(AppError) as e:
            run(
                pool.build_pool(
                    make_settings(track=track),
                    on_progress=on_progress,
                    overrides=None,
                    draft_id=uuid.uuid4(),
                )
            )
    finally:
        get_settings.cache_clear()
    assert (e.value.status, e.value.code) == (503, "AI_PROVIDER_NOT_CONFIGURED")
    assert events == []  # fails before any work


def test_students_get_different_papers_from_selected_pool():
    settings = make_settings()
    chosen = [q for q in build(settings) if q.selected]
    a = builder.build_paper(settings, "d1", "cand-a", chosen).items
    b = builder.build_paper(settings, "d1", "cand-b", chosen).items
    assert len(a) == len(b) == sum(settings.counts.values())
    assert [i.id for i in a] != [i.id for i in b]
    selected_ids = {q.id for q in chosen}
    assert {i.id for i in a} | {i.id for i in b} <= selected_ids


# ------------------------------------------------------------- persistence + edit isolation


def test_send_persists_bank_derived_questions_with_source_tag():
    did = uuid.uuid4()
    bank_id = str(uuid.uuid4())
    tech = {
        "id": str(uuid.uuid4()),
        "level": "easy",
        "section": "technical",
        "topic": "Operating Systems / scheduling",
        "style": "concept",
        "text": "Which scheduler?",
        "options": ["a", "b", "c", "d"],
        "answer": 1,
        "why": "Answer key: B. b",
        "source_id": bank_id,
    }
    plain = {**tech, "id": str(uuid.uuid4()), "section": "aptitude", "source_id": None}
    s = RecordingSession([{"id": tech["id"]}, {"id": plain["id"]}], [])
    run(repo.persist_questions(s, did, [tech, plain]))
    sql, params = s.calls[0]
    # existing tags kept, source tag appended (null for generated questions, then removed)
    assert "'paper:' || d.id::text" in sql and "'style:' || r.style" in sql
    assert "'source:' || r.source_id" in sql and "array_remove(" in sql
    assert "source_id text" in sql
    items = json.loads(params["items"])
    assert items[0]["id"] == tech["id"] and items[0]["source_id"] == bank_id
    assert items[1]["source_id"] is None
    # the copy never points questions.source_id (the bank FILE) at the bank: no such column set
    assert "insert into questions (id, kind, body, options, answer, tags," in sql
    assert "source_id," not in sql.split("select")[0]


def test_progress_total_counts_real_questions_not_percent():
    """The bar reads 'done / total questions', so total must be a question count, not 100."""
    settings = make_settings()
    events: list[tuple[int, int]] = []
    build(settings, events=events)
    want = needed(settings)
    f = settings.pool_factor
    coded = sum(want[lv][s] * (f + 1) for lv in LEVELS for s in ("aptitude", "reasoning"))
    tech = sum(want[lv]["technical"] * f + 1 for lv in LEVELS if want[lv]["technical"])
    assert events[0] == (0, coded + tech)
    assert (coded, coded + tech) in events  # code generators finish before the AI starts

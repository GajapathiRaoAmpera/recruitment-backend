"""Pool pipeline: code generators (Aptitude + Reasoning) + Technical (PDF bank for IT, LLM for
Non-IT) + one composer call that marks needed x pool_factor per cell as selected.

Every cell holds needed x (pool_factor + 1) candidates where the source allows; the ones the
composer leaves out stay in the pool with selected=False so HR can add them back in the picker.
"""

import random
import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.papers import agents
from app.papers.agents import LlmClient, OnProgress
from app.papers.builder import needed
from app.papers.generators import generate_questions
from app.papers.schemas import LEVELS, Level, PaperSettings, PoolQuestion, Section
from app.qbank import retrieval
from app.qbank.embeddings import Embedder
from app.qbank.retrieval import BankQuestion, allocate

# coarse progress steps on one fixed total, so the bar never moves backwards
TOTAL = 100
AFTER_GENERATORS = 10
AFTER_TECHNICAL = 80
FETCH_FACTOR = 2  # bank rows fetched per technical candidate, so the seeded sample can vary


def draft_namespace(draft_id: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"paper-draft|{draft_id}")


def _answer_key(b: BankQuestion) -> str:
    return f"Answer key: {'ABCD'[b.answer]}. {b.options[b.answer]}"


def _from_bank(b: BankQuestion, ns: uuid.UUID) -> PoolQuestion:
    # a new id per draft: the copy is edited/persisted on its own, the bank row is never touched
    return PoolQuestion(
        id=str(uuid.uuid5(ns, b.id)),
        level=b.level,
        section="technical",
        topic=f"{b.subject} / {b.topic}" if b.topic else b.subject,
        style="concept",
        text=b.text,
        options=list(b.options),
        answer=b.answer,
        why=b.why.strip() or _answer_key(b),
        source_id=b.id,
    )


def _sample(
    found: list[BankQuestion], n: int, subjects: Mapping[str, int] | None, rng: random.Random
) -> list[BankQuestion]:
    """n of `found`, seeded, split over the found subjects by HR's weights (even when None)."""
    if n <= 0 or not found:
        return []
    by_subject: dict[str, list[BankQuestion]] = {}
    for b in found:
        by_subject.setdefault(b.subject, []).append(b)
    weights = {s: (subjects or {}).get(s, 0) for s in by_subject}
    if sum(weights.values()) <= 0:  # no weights given (or none for the found subjects): even
        weights = dict.fromkeys(by_subject, 1)
    take, _ = allocate(min(n, len(found)), weights, {s: len(v) for s, v in by_subject.items()})
    out: list[BankQuestion] = []
    for s, items in by_subject.items():
        out.extend(rng.sample(items, take[s]))
    return out


async def _bank_technical(
    session: AsyncSession,
    settings: PaperSettings,
    want: Mapping[Level, Mapping[Section, int]],
    draft_id: str,
    embedder: Embedder | None,
) -> list[PoolQuestion]:
    per_cell = {lv: want[lv]["technical"] * (settings.pool_factor + 1) for lv in LEVELS}
    found = await retrieval.retrieve_technical(
        session,
        track=settings.track,
        per_level={lv: n * FETCH_FACTOR for lv, n in per_cell.items()},
        subjects=settings.subjects,
        focus=settings.focus,
        embedder=embedder,
    )
    by_level: dict[str, list[BankQuestion]] = {}
    for b in found.questions:
        by_level.setdefault(b.level, []).append(b)
    missing = {
        f"{lv}/technical": want[lv]["technical"] - len(by_level.get(lv, []))
        for lv in LEVELS
        if len(by_level.get(lv, [])) < want[lv]["technical"]
    }
    if missing:
        raise AppError(
            422,
            "BANK_TOO_SMALL",
            "The question banks do not have enough approved Technical questions for this paper. "
            "Upload another PDF bank or approve more questions, then generate again.",
            {"missing": missing},
        )
    rng = random.Random(f"{draft_id}|technical")
    ns = draft_namespace(draft_id)
    return [
        _from_bank(b, ns)
        for lv in LEVELS
        for b in _sample(by_level.get(lv, []), per_cell[lv], settings.subjects, rng)
    ]


async def build_pool(
    session: AsyncSession,
    settings: PaperSettings,
    *,
    on_progress: OnProgress,
    overrides: Mapping[str, str] | None,
    draft_id: uuid.UUID | str,
    embedder: Embedder | None = None,
    composer_client: LlmClient | None = None,
    generator_client: LlmClient | None = None,
    verifier_client: LlmClient | None = None,
) -> list[PoolQuestion]:
    did = str(draft_id)
    # clients first: a missing key fails before any work (IT needs only Gemini)
    composer = composer_client or agents.GeminiClient()
    if settings.track == "non_it":
        generator = generator_client or agents.GeminiClient()
        verifier = verifier_client or agents.GroqClient()

    want = needed(settings)
    factor = settings.pool_factor
    await on_progress(0, TOTAL)

    pool: list[PoolQuestion] = []
    for lv in LEVELS:
        for section in ("aptitude", "reasoning"):
            n = want[lv][section] * (factor + 1)
            pool += generate_questions(section, lv, n, seed=f"{did}|{section}|{lv}")
    await on_progress(AFTER_GENERATORS, TOTAL)

    if settings.track == "it":
        pool += await _bank_technical(session, settings, want, did, embedder)
    else:
        span = AFTER_TECHNICAL - AFTER_GENERATORS

        async def tech_progress(done: int, total: int) -> None:
            await on_progress(AFTER_GENERATORS + (done * span // total if total else 0), TOTAL)

        pool += await agents.generate_pool(
            generator,
            verifier,
            settings,
            on_progress=tech_progress,
            overrides=overrides,
            sections=("technical",),
        )
    await on_progress(AFTER_TECHNICAL, TOTAL)

    have: dict[tuple[Level, Section], int] = {}
    for q in pool:
        have[(q.level, q.section)] = have.get((q.level, q.section), 0) + 1
    target = {cell: min(want[cell[0]][cell[1]] * factor, n) for cell, n in have.items()}
    keep = await agents.compose_selection(composer, pool, target, overrides=overrides)
    pool = [q.model_copy(update={"selected": q.id in keep}) for q in pool]
    await on_progress(TOTAL, TOTAL)
    return pool

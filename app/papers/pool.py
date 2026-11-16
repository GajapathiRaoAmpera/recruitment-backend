"""Pool pipeline: code generators (Aptitude + Reasoning) + Technical written for the drive's role
by an NVIDIA model (IT and Non-IT; HR checks the answers in review) + one composer call that
marks needed x pool_factor per cell as selected.

Aptitude and Reasoning cells hold needed x (pool_factor + 1) candidates; the ones the composer
leaves out stay in the pool with selected=False so HR can add them back in the picker.
"""

import uuid
from collections.abc import Mapping

from app.papers import agents
from app.papers.agents import LlmClient, OnProgress
from app.papers.builder import needed
from app.papers.generators import generate_questions
from app.papers.schemas import LEVELS, Level, PaperSettings, PoolQuestion, Section


async def build_pool(
    settings: PaperSettings,
    *,
    on_progress: OnProgress,
    overrides: Mapping[str, str] | None,
    draft_id: uuid.UUID | str,
    composer_client: LlmClient | None = None,
    generator_client: LlmClient | None = None,
) -> list[PoolQuestion]:
    did = str(draft_id)
    # clients first: a missing key fails before any work
    generator = generator_client or agents.NvidiaClient(temperature=0.7)  # varied questions
    composer = composer_client or generator

    want = needed(settings)
    factor = settings.pool_factor
    # progress counts real questions: generated Aptitude/Reasoning + Technical asked of the AI
    # (its top-up reserve included); done == total while the composer runs. Fixed from the start.
    coded = sum(want[lv][s] * (factor + 1) for lv in LEVELS for s in ("aptitude", "reasoning"))
    total = coded + agents.progress_total(settings, ("technical",))
    await on_progress(0, total)

    pool: list[PoolQuestion] = []
    for lv in LEVELS:
        for section in ("aptitude", "reasoning"):
            n = want[lv][section] * (factor + 1)
            pool += generate_questions(section, lv, n, seed=f"{did}|{section}|{lv}")
    await on_progress(coded, total)

    async def tech_progress(done: int, _tech_total: int) -> None:
        await on_progress(coded + done, total)

    pool += await agents.generate_pool(
        generator,
        settings,
        on_progress=tech_progress,
        overrides=overrides,
        sections=("technical",),
    )
    await on_progress(total, total)  # now composing the selection

    have: dict[tuple[Level, Section], int] = {}
    for q in pool:
        have[(q.level, q.section)] = have.get((q.level, q.section), 0) + 1
    target = {cell: min(want[cell[0]][cell[1]] * factor, n) for cell, n in have.items()}
    keep = await agents.compose_selection(composer, pool, target, overrides=overrides)
    pool = [q.model_copy(update={"selected": q.id in keep}) for q in pool]
    await on_progress(total, total)
    return pool

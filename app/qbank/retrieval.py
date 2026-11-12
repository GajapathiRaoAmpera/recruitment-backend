"""Technical-question retrieval from the question banks (the retrieval half of RAG).

Only approved questions of active, ready banks of the requested track are candidates (the filter
lives in repo.search_questions). Per level, the wanted count is split across subjects by weight
(largest remainder; equal split when no weights are given). A subject without enough stock hands
its share to subjects that have spare stock (again by weight); whatever still cannot be covered is
reported as shortfall - questions are never invented.

With a focus text and an embedder, candidates inside each (level, subject) cell are ranked by
cosine distance to the embedded focus; questions without a vector of the embedder's model (bank
not embedded, embedding failed/skipped, edited and not re-embedded) come after the ranked ones.
Without focus the order is deterministic: bank upload time (created_at), then question id - so
the same request always returns the same questions. Callers that want variety (Task 6, papers
that differ per student) must OVERSAMPLE (ask for more than they need) and shuffle themselves.

Retrieval reports how focus went, so the UI can say so instead of hiding it:
  ranked / unranked = returned questions with / without a focus distance (both 0 without focus);
  focus_applied = focus was used and EVERY returned question was ranked by it;
  focus_partial = focus was used for some returned questions but not all (mixed banks).
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.papers.builder import split_by_percent
from app.papers.schemas import LEVELS, Level, Track
from app.qbank import repo
from app.qbank.embeddings import Embedder

log = logging.getLogger(__name__)

ANY_SUBJECT = "*"  # shortfall key when the bank has no subject at all for the request


@dataclass(frozen=True)
class BankQuestion:
    id: str
    subject: str
    topic: str
    level: Level
    text: str
    options: list[str]
    answer: int
    why: str


@dataclass(frozen=True)
class Retrieval:
    questions: list[BankQuestion] = field(default_factory=list)
    shortfall: dict[str, int] = field(default_factory=dict)  # {"level/subject": missing}
    focus_applied: bool = False  # focus ranked every returned question
    focus_partial: bool = False  # focus ranked some returned questions, not all
    ranked: int = 0  # returned questions that had a focus distance
    unranked: int = 0  # returned questions without one (only counted when focus was used)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


def _to_question(r: Mapping[str, Any]) -> BankQuestion:
    return BankQuestion(
        id=str(r["id"]),
        subject=r["subject"],
        topic=r["topic"] or "",
        level=r["level"],
        text=r["text"],
        options=[str(o) for o in _json(r["options"])],
        answer=int(_json(r["answer"])),
        why=r["explanation"] or "",
    )


def _check_args(per_level: Mapping[str, int], subjects: Mapping[str, int] | None) -> None:
    unknown = set(per_level) - set(LEVELS)
    if unknown:
        raise ValueError(f"unknown levels {sorted(unknown)}")
    if any(n < 0 for n in per_level.values()):
        raise ValueError("per_level counts must be >= 0")
    if subjects is not None:
        if any(w < 0 for w in subjects.values()):
            raise ValueError("subject weights must be >= 0")
        if sum(subjects.values()) <= 0:
            raise ValueError("subject weights must have a positive sum")


def allocate(
    total: int, weights: Mapping[str, int], stock: Mapping[str, int]
) -> tuple[dict[str, int], dict[str, int]]:
    """Split `total` over subjects by weight, capped by stock; shares a subject cannot cover go to
    subjects with spare stock (by weight). Returns (take per subject, shortfall per subject);
    the shortfall is charged to the subjects whose own share went uncovered, biggest share
    first, and sums to total - sum(take)."""
    share = split_by_percent(total, dict(weights))
    take = {s: min(share[s], stock.get(s, 0)) for s in weights}
    deficit = total - sum(take.values())
    while deficit > 0:
        spare = {s: stock.get(s, 0) - take[s] for s in weights}
        spare_weights = {s: weights[s] for s in weights if spare[s] > 0}
        if not spare_weights:
            break
        # the split sums to deficit > 0 over subjects that all have spare, so each round moves
        # at least one question and the loop ends
        for s, n in split_by_percent(deficit, spare_weights).items():
            add = min(n, spare[s])
            take[s] += add
            deficit -= add
    short: dict[str, int] = {}
    for s in sorted(weights, key=lambda k: -share[k]):  # stable: ties keep weight order
        if deficit <= 0:
            break
        missing = min(share[s] - min(share[s], stock.get(s, 0)), deficit)
        if missing > 0:
            short[s] = missing
            deficit -= missing
    return take, short


async def _focus_vector(focus: str | None, embedder: Embedder | None) -> list[float] | None:
    query = " ".join((focus or "").split())
    if not query or embedder is None:
        return None
    try:
        (vec,) = await embedder.embed([query], task="query")
    except Exception as e:  # ranking is optional; the caller sees focus_applied=False
        log.warning("embedding the focus text failed (%s)", type(e).__name__)
        return None
    return list(vec)


async def retrieve_technical(
    session: AsyncSession,
    *,
    track: Track,
    per_level: Mapping[Level, int],
    subjects: Mapping[str, int] | None,
    focus: str | None,
    embedder: Embedder | None,
) -> Retrieval:
    """Bank questions for the Technical section. `per_level` = how many per level (missing
    levels = 0); `subjects` = {subject: weight} (weights > 0 used; None = equal split over the
    subjects the banks have). Raises ValueError on bad counts / weights."""
    _check_args(per_level, subjects)
    levels = [lv for lv in LEVELS if per_level.get(lv, 0) > 0]
    if not levels:
        return Retrieval()
    weighted = {s: w for s, w in (subjects or {}).items() if w > 0}
    query_vec = await _focus_vector(focus, embedder)
    rows = await repo.search_questions(
        session,
        track=track,
        levels=levels,
        subjects=list(weighted) if subjects is not None else None,
        query_vec=query_vec,
        # one subject may have to cover a whole level, so fetch up to that many per cell
        per_cell=max(per_level[lv] for lv in levels),
        model=embedder.model if query_vec is not None and embedder is not None else None,
    )
    cells: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for r in rows:  # rows arrive ranked within each cell
        cells.setdefault((r["level"], r["subject"]), []).append(r)
    if subjects is None:
        weighted = {s: 1 for s in dict.fromkeys(r["subject"] for r in rows)}

    picked: list[Mapping[str, Any]] = []
    shortfall: dict[str, int] = {}
    for lv in levels:
        want = per_level[lv]
        if not weighted:
            shortfall[f"{lv}/{ANY_SUBJECT}"] = want
            continue
        stock = {s: len(cells.get((lv, s), [])) for s in weighted}
        take, short = allocate(want, weighted, stock)
        for s in weighted:
            picked.extend(cells.get((lv, s), [])[: take[s]])
        shortfall.update({f"{lv}/{s}": n for s, n in short.items()})

    ranked = unranked = 0
    if query_vec is not None:
        ranked = sum(1 for r in picked if r.get("distance") is not None)
        unranked = len(picked) - ranked
    return Retrieval(
        questions=[_to_question(r) for r in picked],
        shortfall=shortfall,
        focus_applied=ranked > 0 and unranked == 0,
        focus_partial=ranked > 0 and unranked > 0,
        ranked=ranked,
        unranked=unranked,
    )

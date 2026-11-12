"""Pure builder for the Round 1 paper: split, seeded draw, checks, overlap."""

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.papers.schemas import (
    LEVELS,
    SECTIONS,
    Check,
    KeyItem,
    Level,
    PaperSettings,
    PoolQuestion,
    Section,
)


@dataclass(frozen=True)
class BuiltPaper:
    items: list[KeyItem]
    # "<level>/<section>" -> how many questions the pool could not supply
    missing: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Overlap:
    shared: int
    same_position: int
    total: int


def split_by_percent[K: str](total: int, weights: Mapping[K, int]) -> dict[K, int]:
    """Largest-remainder split of `total` by integer weights; always sums to total."""
    weight_sum = sum(weights.values())
    if total < 0 or weight_sum <= 0 or any(w < 0 for w in weights.values()):
        raise ValueError("total must be >= 0 and weights non-negative with a positive sum")
    keys = list(weights)
    base = {k: total * weights[k] // weight_sum for k in keys}
    remainder = {k: total * weights[k] % weight_sum for k in keys}
    left = total - sum(base.values())
    # biggest remainder first; ties keep the original key order (sort is stable)
    for k in sorted(keys, key=lambda key: -remainder[key])[:left]:
        base[k] += 1
    return base


def needed(settings: PaperSettings) -> dict[Level, dict[Section, int]]:
    return {
        level: split_by_percent(settings.counts[level], {s: settings.mix[s] for s in SECTIONS})
        for level in LEVELS
    }


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _to_key_item(q: PoolQuestion, options: list[str], answer: int) -> KeyItem:
    return KeyItem(
        id=q.id,
        level=q.level,
        section=q.section,
        topic=q.topic,
        text=q.text,
        options=options,
        answer=answer,
        why=q.why,
        fixed_options=q.fixed_options,
        edited=q.edited,
    )


def build_paper(
    settings: PaperSettings,
    drive_ref: str,
    candidate_ref: str,
    pool: Sequence[PoolQuestion],
) -> BuiltPaper:
    ref = candidate_ref if settings.different_per_student else "fixed"
    rng = random.Random(f"{drive_ref}|{settings.track}|{ref}")
    want = needed(settings)

    items: list[KeyItem] = []
    missing: dict[str, int] = {}
    used_ids: set[str] = set()
    used_text: set[str] = set()

    for level in LEVELS:
        for section in SECTIONS:
            count = want[level][section]
            if count == 0:
                continue
            candidates: list[PoolQuestion] = []
            seen: set[str] = set()
            seen_ids: set[str] = set()
            for q in pool:
                if q.level != level or q.section != section:
                    continue
                key = _norm(q.text)
                if q.id in used_ids or key in used_text or key in seen or q.id in seen_ids:
                    continue
                seen.add(key)
                seen_ids.add(q.id)
                candidates.append(q)
            picked = rng.sample(candidates, min(count, len(candidates)))
            if len(picked) < count:
                missing[f"{level}/{section}"] = count - len(picked)
            for q in picked:
                used_ids.add(q.id)
                used_text.add(_norm(q.text))
                options, answer = list(q.options), q.answer
                if settings.shuffle_options and not q.fixed_options:
                    order = rng.sample(range(len(q.options)), len(q.options))
                    options = [q.options[i] for i in order]
                    answer = order.index(q.answer) if q.answer in order else q.answer
                items.append(_to_key_item(q, options, answer))

    if settings.shuffle_questions:
        rng.shuffle(items)
    return BuiltPaper(items=items, missing=missing)


def overlap(a: Sequence[KeyItem], b: Sequence[KeyItem]) -> Overlap:
    ids_b = {i.id for i in b}
    shared = sum(1 for i in a if i.id in ids_b)
    same_position = sum(1 for x, y in zip(a, b, strict=False) if x.id == y.id)
    return Overlap(shared=shared, same_position=same_position, total=len(a))


def _bad_question_reason(q: PoolQuestion) -> str | None:
    if len(q.options) != 4:
        return "needs exactly 4 options"
    if any(not o.strip() for o in q.options):
        return "has a blank option"
    if len({_norm(o) for o in q.options}) != 4:
        return "has duplicate options"
    if not 0 <= q.answer < 4:
        return "answer index out of range"
    if not q.why.strip():
        return "missing worked solution"
    if not q.text.strip():
        return "blank question text"
    return None


def run_checks(
    settings: PaperSettings, drive_ref: str, pool: Sequence[PoolQuestion]
) -> list[Check]:
    checks: list[Check] = []

    try:
        PaperSettings.model_validate(settings.model_dump())
        checks.append(Check(id="settings_valid", label="Settings valid", ok=True, detail="OK"))
    except ValidationError as exc:
        checks.append(
            Check(
                id="settings_valid",
                label="Settings valid",
                ok=False,
                detail=str(exc.errors()[0].get("msg", "invalid settings")),
            )
        )

    sample = build_paper(settings, drive_ref, "sample", pool)
    if sample.missing:
        detail = "Short of: " + ", ".join(
            f"{k.replace('/', ' ')} by {n}" for k, n in sample.missing.items()
        )
    else:
        detail = f"Pool of {len(pool)} covers all {len(sample.items)} questions"
    checks.append(
        Check(id="pool_covers", label="Pool covers the paper", ok=not sample.missing, detail=detail)
    )

    ids = [q.id for q in pool]
    texts = [_norm(q.text) for q in pool]
    dup_ids = len(ids) - len(set(ids))
    dup_text = len(texts) - len(set(texts))
    paper_ids = [i.id for i in sample.items]
    paper_dups = len(paper_ids) - len(set(paper_ids))
    repeats_ok = not (dup_ids or dup_text or paper_dups)
    checks.append(
        Check(
            id="no_repeats",
            label="No repeated questions",
            ok=repeats_ok,
            detail="No repeats"
            if repeats_ok
            else f"{dup_ids} duplicate ids, {dup_text} duplicate texts in the pool",
        )
    )

    bad = [(q.id, r) for q in pool if (r := _bad_question_reason(q))]
    checks.append(
        Check(
            id="answers_present",
            label="Every question has a valid answer and explanation",
            ok=not bad,
            detail="All questions valid"
            if not bad
            else "; ".join(f"{qid}: {r}" for qid, r in bad[:5]),
        )
    )

    if not settings.different_per_student:
        differ, detail = True, "Same paper for everyone by design"
    else:
        a = build_paper(settings, drive_ref, "sample-a", pool).items
        b = build_paper(settings, drive_ref, "sample-b", pool).items
        ov = overlap(a, b)
        differ = ov.shared < ov.total
        detail = f"{ov.shared}/{ov.total} questions shared between two sample students"
        if not differ:
            detail += (
                "; the pool is too small for every student to get different questions "
                "(raise pool_factor)"
            )
    checks.append(
        Check(id="students_differ", label="Students get different papers", ok=differ, detail=detail)
    )
    return checks

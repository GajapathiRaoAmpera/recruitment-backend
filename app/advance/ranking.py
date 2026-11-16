"""Ranking math, no database: percent of each student's own max marks, then rank.

percent = marks / max_marks x 100, rounded to 2 decimals (what HR sees and what
round_candidates.score stores), and the cut-off compares that rounded value: equal passes.
Rank 1..n over finished attempts: percent desc, then time_taken_sec asc, then student_id
(only so the order never changes between two calls).
"""

from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal

from app.advance.schemas import Counts, RankingEntry

CENT = Decimal("0.01")
FINISHED = ("submitted", "expired")


def to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def percent(marks, max_marks) -> Decimal:
    m, mx = to_decimal(marks) or Decimal(0), to_decimal(max_marks) or Decimal(0)
    if mx <= 0:
        return Decimal("0.00")  # a paper with no marks to earn can't be passed on marks
    return (m * 100 / mx).quantize(CENT, rounding=ROUND_HALF_UP)


def cut_off(threshold) -> Decimal | None:
    t = to_decimal(threshold)
    return None if t is None else t.quantize(CENT, rounding=ROUND_HALF_UP)


def kind(attempt_status: str | None) -> str:
    """finished | still_writing | locked | absent."""
    if attempt_status in FINISHED:
        return "finished"
    if attempt_status == "in_progress":
        return "still_writing"
    if attempt_status == "locked":
        return "locked"
    return "absent"  # no attempt, or never started


def rank(rows: Iterable[Mapping], threshold) -> tuple[list[RankingEntry], Counts]:
    """rows: candidate_id, student_id, name, attempt_status, marks, max_marks, time_taken_sec.
    Past-deadline attempts must already be finalized (they would count as still writing)."""
    t = cut_off(threshold)
    finished, others = [], []
    for r in rows:
        k = kind(r["attempt_status"])
        entry = {
            "candidate_id": r["candidate_id"],
            "student_id": r["student_id"],
            "name": r["name"],
            "attempt_status": r["attempt_status"],
            "marks": r["marks"],
            "max_marks": r["max_marks"],
            "percent": None,
            "rank": None,
            "time_taken_sec": r["time_taken_sec"],
            "would_pass": None,
            "_kind": k,
        }
        if k == "finished":
            entry["marks"] = to_decimal(r["marks"]) or Decimal(0)  # submit stores 0, not null
            entry["percent"] = percent(entry["marks"], r["max_marks"])
            if t is not None:
                entry["would_pass"] = entry["percent"] >= t
            finished.append(entry)
        else:
            if k == "absent" and t is not None:
                entry["would_pass"] = False
            others.append(entry)

    big = 1 << 62  # a finished attempt with no time recorded goes last among equal percents
    finished.sort(
        key=lambda e: (
            -e["percent"],
            e["time_taken_sec"] if e["time_taken_sec"] is not None else big,
            e["student_id"],
        )
    )
    for i, e in enumerate(finished, start=1):
        e["rank"] = i
    order = {"still_writing": 0, "locked": 1, "absent": 2}
    others.sort(key=lambda e: (order[e["_kind"]], e["name"].lower(), e["student_id"]))

    kinds = [e["_kind"] for e in finished + others]
    counts = Counts(
        pass_=None if t is None else sum(1 for e in finished if e["would_pass"]),
        fail=None if t is None else sum(1 for e in finished if not e["would_pass"]),
        absent=kinds.count("absent"),
        still_writing=kinds.count("still_writing"),
        locked=kinds.count("locked"),
    )
    entries = [
        RankingEntry(**{k: v for k, v in e.items() if k != "_kind"}) for e in finished + others
    ]
    return entries, counts

"""Shared types and helpers for the computed-answer generators."""

import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from app.papers.schemas import Level

Facts = dict[str, Any]


@dataclass(frozen=True)
class GeneratedQuestion:
    topic: str
    text: str
    options: list[str]
    answer: int
    why: str
    fixed_options: bool = False


Generator = Callable[[random.Random, Level], GeneratedQuestion]
# A topic builder returns the question plus the structured facts behind it (for tests).
Builder = Callable[[random.Random, Level], tuple[GeneratedQuestion, Facts]]


def public(builder: Builder) -> Generator:
    """The caller-facing generator: the question only, never the facts."""

    def generate(rng: random.Random, level: Level) -> GeneratedQuestion:
        return builder(rng, level)[0]

    return generate


def fmt_num(value: Fraction | int) -> str:
    """Integers print plainly; other values print with at most 2 decimals."""
    value = Fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{float(round(value, 2)):.2f}".rstrip("0").rstrip(".")


def fmt_frac(value: Fraction) -> str:
    """A fraction in lowest terms, e.g. 1/20 (never 90/1800)."""
    value = Fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def plural(count: Fraction | int | str, singular: str, plural_form: str | None = None) -> str:
    """Count plus noun: "1 day", "2 days", "1.5 days" (singular only for exactly one)."""
    shown = count if isinstance(count, str) else fmt_num(count)
    word = singular if Fraction(shown) == 1 else (plural_form or singular + "s")
    return f"{shown} {word}"


def place_answer(
    rng: random.Random, answer: str, distractors: Iterable[str]
) -> tuple[list[str], int]:
    """Three distinct distractors (in preference order) around the answer at a random index."""
    picked: list[str] = []
    seen = {answer.casefold()}
    for d in distractors:
        if d.casefold() in seen:
            continue
        seen.add(d.casefold())
        picked.append(d)
        if len(picked) == 3:
            break
    if len(picked) < 3:
        raise ValueError("not enough distinct distractors")
    rng.shuffle(picked)
    index = rng.randrange(4)
    picked.insert(index, answer)
    return picked, index

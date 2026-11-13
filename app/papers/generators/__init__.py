"""Computed-answer question generators for the Aptitude and Reasoning sections.

The LLM never writes these questions: each generator builds the numbers or the puzzle, computes
the answer in code and offers plausible slips as distractors.
"""

import random
import uuid
from typing import Literal

from app.papers.generators.aptitude import APTITUDE
from app.papers.generators.common import Builder, GeneratedQuestion, Generator
from app.papers.generators.reasoning import REASONING
from app.papers.schemas import Level, PoolQuestion

__all__ = [
    "APTITUDE",
    "Builder",
    "MAX_COUNT",
    "REASONING",
    "GeneratedQuestion",
    "Generator",
    "generate_questions",
]

MAX_COUNT = 2000
_MAX_MISSES_PER_TOPIC = 50  # consecutive duplicate texts tolerated per topic before giving up


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def generate_questions(
    section: Literal["aptitude", "reasoning"], level: Level, count: int, seed: str
) -> list[PoolQuestion]:
    """`count` questions with unique texts, topics in rotation, fully determined by `seed`.

    Raises ValueError (and only ValueError) when `count` is out of range or the generators
    cannot supply enough distinct questions. A generator that gives up on one attempt
    (RuntimeError, e.g. an ordering puzzle that never became unique) counts as a failed
    attempt and the rotation moves on to the next topic.
    """
    if section == "aptitude":
        registry = APTITUDE
    elif section == "reasoning":
        registry = REASONING
    else:
        raise ValueError(f"no generators for section {section!r}")
    if not 0 <= count <= MAX_COUNT:
        raise ValueError(f"count must be between 0 and {MAX_COUNT}")

    rng = random.Random(seed)
    topics = list(registry)
    offset = rng.randrange(len(topics))
    max_misses = _MAX_MISSES_PER_TOPIC * len(topics)
    out: list[PoolQuestion] = []
    seen: set[str] = set()
    turn = misses = 0
    while len(out) < count:
        topic = topics[(offset + turn) % len(topics)]
        turn += 1
        try:
            g = registry[topic](rng, level)
        except RuntimeError:
            g = None
        if g is None or _norm(g.text) in seen:
            misses += 1
            if misses > max_misses:
                raise ValueError(
                    f"only {len(out)} distinct {section} questions could be generated "
                    f"at level {level}; {count} were requested"
                )
            continue
        misses = 0
        seen.add(_norm(g.text))
        out.append(
            PoolQuestion(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{seed}|{g.topic}|{g.text}")),
                level=level,
                section=section,
                topic=g.topic,
                style=section,
                text=g.text,
                options=list(g.options),
                answer=g.answer,
                why=g.why,
                fixed_options=g.fixed_options,
                verified=True,
                selected=True,
            )
        )
    return out

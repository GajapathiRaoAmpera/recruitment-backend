"""Aptitude generators: every answer is re-derived here by an independent solver.

The solvers below never call generator code: they read the structured facts the generator
used (via `aptitude._facts`, a test-only hook that replays the same seeded rng) and recompute
the answer with a different formula or by brute force.
"""

import random
import re
from fractions import Fraction

import pytest

from app.papers.generators import (
    APTITUDE,
    REASONING,
    GeneratedQuestion,
    aptitude,
    generate_questions,
)
from app.papers.schemas import LEVELS, PoolQuestion

REQUIRED = {
    "percentage",
    "profit_loss",
    "ratio",
    "time_work",
    "speed_distance",
    "simple_interest",
    "average",
    "mixture",
}
SEEDS = [f"seed-{i}" for i in range(25)]
NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _value(option: str) -> Fraction:
    found = NUM.findall(option)
    assert len(found) == 1, f"option should carry exactly one number: {option!r}"
    return Fraction(found[0])


# ---------------------------------------------------------------- independent solvers


def _percentage(level, f):
    if level == "easy":
        return Fraction(f["n"]) * f["p"] / 100
    if level == "medium":
        value = Fraction(f["n"])
        value += value * f["up"] / 100
        value -= value * f["down"] / 100
        return value
    # reverse percentage: brute force the original price
    for original in range(1, 200_001):
        if original * (100 - f["discount"]) == f["price"] * 100:
            return Fraction(original)
    raise AssertionError("no original price found")


def _profit_loss(level, f):
    cost = Fraction(f["cost"])
    if level == "easy":
        return cost + cost * f["profit_pct"] / 100
    if level == "medium":
        return (f["sell"] - cost) / cost * 100
    marked = cost * (100 + f["markup"]) / 100
    sold = marked - marked * f["discount"] / 100
    return (sold - cost) / cost * 100


def _ratio(level, f):
    if level == "easy":
        for part in range(1, f["total"] + 1):
            if part * (f["a"] + f["b"]) == f["total"]:
                return Fraction(part * f["a"])
        raise AssertionError("no part found")
    if level == "medium":
        hits = [
            x
            for x in range(1, 5000)
            if (f["a"] * x + f["add"]) * f["d"] == (f["b"] * x + f["add"]) * f["c"]
        ]
        assert len(hits) == 1, hits
        return Fraction(min(f["a"], f["b"]) * hits[0])
    shorter_m = Fraction(f["metres"]) * min(f["a"], f["b"]) / (f["a"] + f["b"])
    return shorter_m * 100  # centimetres


def _time_work(level, f):
    if level == "easy":
        man_days = f["workers"] * f["days"]
        return Fraction(man_days, f["new_workers"])
    if level == "medium":
        # brute force whole days, then exact fractional check: t/a + t/b == 1
        hits = [t for t in range(1, 1000) if Fraction(t, f["a"]) + Fraction(t, f["b"]) == 1]
        assert len(hits) == 1, hits
        return Fraction(hits[0])
    done = f["together_days"] * (Fraction(1, f["a"]) + Fraction(1, f["b"]))
    rest_days = (1 - done) * f["b"]
    assert rest_days > 0
    return f["together_days"] + rest_days


def _speed_distance(level, f):
    if level == "easy":
        return Fraction(f["distance"], f["hours"])
    if level == "medium":
        total_time = Fraction(f["distance"], f["u"]) + Fraction(f["distance"], f["v"])
        return 2 * f["distance"] / total_time
    metres_per_second = Fraction(f["kmph"] * 1000, 3600)
    return (f["train_m"] + f["platform_m"]) / metres_per_second


def _simple_interest(level, f):
    principal = Fraction(f["principal"])
    if level == "advanced":
        monthly = principal * f["rate"] / 100 / 12
        return monthly * f["months"]
    interest = sum((principal * f["rate"] / 100 for _ in range(f["years"])), Fraction(0))
    return interest if level == "easy" else principal + interest


def _average(level, f):
    if level == "easy":
        return Fraction(sum(f["numbers"]), len(f["numbers"]))
    if level == "medium":
        return Fraction(f["count"] * f["mean"] - f["removed"], f["count"] - 1)
    return Fraction(f["n1"] * f["a1"] + f["n2"] * f["a2"], f["n1"] + f["n2"])


def _mixture(level, f):
    if level == "easy":
        return Fraction(f["litres"]) * f["pct"] / 100
    if level == "medium":
        # acid stays fixed: litres * pct == (litres + water) * target_pct
        hits = [
            w
            for w in range(0, 5000)
            if f["litres"] * f["pct"] == (f["litres"] + w) * f["target_pct"]
        ]
        assert len(hits) == 1, hits
        return Fraction(hits[0])
    milk, total = Fraction(f["litres"]), f["litres"]
    for _ in range(f["times"]):
        milk -= milk * f["removed"] / total
    return milk


SOLVERS = {
    "percentage": _percentage,
    "profit_loss": _profit_loss,
    "ratio": _ratio,
    "time_work": _time_work,
    "speed_distance": _speed_distance,
    "simple_interest": _simple_interest,
    "average": _average,
    "mixture": _mixture,
}


# ---------------------------------------------------------------- tests


def test_required_topics_present():
    assert REQUIRED <= set(APTITUDE)
    assert len(APTITUDE) >= 8
    assert set(SOLVERS) == set(APTITUDE)


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("topic", sorted(REQUIRED))
def test_shape(topic, level):
    for seed in SEEDS:
        q = APTITUDE[topic](random.Random(seed), level)
        assert isinstance(q, GeneratedQuestion)
        assert q.topic == topic
        assert q.text.strip() and q.why.strip()
        assert len(q.options) == 4
        assert len({o.strip().casefold() for o in q.options}) == 4
        assert all(o.strip() for o in q.options)
        assert 0 <= q.answer < 4
        assert q.fixed_options is False


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("topic", sorted(REQUIRED))
def test_answer_matches_independent_solver(topic, level):
    for seed in SEEDS:
        q = APTITUDE[topic](random.Random(seed), level)
        facts = aptitude._facts(topic, random.Random(seed), level)
        expected = SOLVERS[topic](level, facts)
        values = [_value(o) for o in q.options]
        assert values[q.answer] == expected, (seed, q.text, q.options, facts)
        # the answer appears once; distractors are positive and differ numerically
        assert values.count(expected) == 1
        assert len(set(values)) == 4
        assert all(v > 0 for v in values)
        # every given number is visible in the question text
        for key, value in facts.items():
            shown = value if isinstance(value, list) else [value]
            for v in shown:
                if isinstance(v, int):
                    assert re.search(rf"(?<![\d.]){v}(?![\d])", q.text), (key, v, q.text)


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("topic", sorted(REQUIRED))
def test_same_seed_same_question(topic, level):
    a = APTITUDE[topic](random.Random("same"), level)
    b = APTITUDE[topic](random.Random("same"), level)
    assert a == b


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("topic", sorted(REQUIRED))
def test_seeds_give_variety(topic, level):
    texts = {APTITUDE[topic](random.Random(f"v{i}"), level).text for i in range(20)}
    assert len(texts) >= 10


def test_difficulty_scales():
    # advanced percentage/interest/speed questions require a conversion or a reverse step
    adv = APTITUDE["simple_interest"](random.Random("x"), "advanced")
    assert "month" in adv.text
    adv = APTITUDE["speed_distance"](random.Random("x"), "advanced")
    assert "km/h" in adv.text and "second" in adv.text
    adv = APTITUDE["ratio"](random.Random("x"), "advanced")
    assert "centimetre" in adv.text


@pytest.mark.parametrize("level", LEVELS)
def test_generate_questions_count_unique_and_stable(level):
    qs = generate_questions("aptitude", level, 30, "drive-1|aptitude")
    assert len(qs) == 30
    assert all(isinstance(q, PoolQuestion) for q in qs)
    assert len({q.id for q in qs}) == 30
    assert len({" ".join(q.text.split()).casefold() for q in qs}) == 30
    assert {q.topic for q in qs} == set(APTITUDE)  # topics rotate
    for q in qs:
        assert q.level == level and q.section == "aptitude" and q.style == "aptitude"
        assert q.selected is True and q.verified is True and q.fixed_options is False
    again = generate_questions("aptitude", level, 30, "drive-1|aptitude")
    assert [q.model_dump() for q in again] == [q.model_dump() for q in qs]
    other = generate_questions("aptitude", level, 30, "drive-2|aptitude")
    assert {q.id for q in other} != {q.id for q in qs}


def test_generate_questions_id_formula():
    import uuid

    q = generate_questions("aptitude", "easy", 1, "s")[0]
    assert q.id == str(uuid.uuid5(uuid.NAMESPACE_URL, f"s|{q.topic}|{q.text}"))


def test_generate_questions_zero_and_impossible():
    assert generate_questions("aptitude", "easy", 0, "s") == []
    with pytest.raises(ValueError):
        generate_questions("aptitude", "easy", -1, "s")
    with pytest.raises(ValueError):
        generate_questions("aptitude", "easy", 10**7, "s")


def test_generate_questions_raises_when_unique_questions_run_out(monkeypatch):
    fixed = GeneratedQuestion(
        topic="percentage", text="Same text", options=["1", "2", "3", "4"], answer=0, why="w"
    )
    for name in list(APTITUDE):
        monkeypatch.setitem(APTITUDE, name, lambda rng, level: fixed)
    assert len(generate_questions("aptitude", "easy", 1, "s")) == 1
    with pytest.raises(ValueError):
        generate_questions("aptitude", "easy", 2, "s")


def test_reasoning_registry_exported():
    assert len(REASONING) >= 7


WIDE_SEEDS = [f"wide-{i}" for i in range(150)]


def _wide(topic, level):
    for seed in WIDE_SEEDS:
        yield (
            APTITUDE[topic](random.Random(seed), level),
            aptitude._facts(topic, random.Random(seed), level),
        )


def test_no_broken_plurals_anywhere():
    units = "days|litres|seconds|hours|years|months|workers|students|numbers|metres|places"
    bad = re.compile(rf"(?<![\d.])1 (more )?({units})\b|\(s\)")
    for topic in APTITUDE:
        for level in LEVELS:
            for q, _f in _wide(topic, level):
                for chunk in (q.text, q.why, *q.options):
                    assert not bad.search(chunk), (topic, level, chunk)


def test_known_singular_cases_read_naturally():
    # seeds the reviewer found rendering "1 days" / "1 litres"
    for topic, seed, level, word in [
        ("time_work", "s13", "easy", "day"),
        ("mixture", "s17", "easy", "litre"),
        ("time_work", "s47", "advanced", "day"),
    ]:
        q = APTITUDE[topic](random.Random(seed), level)
        joined = " ".join([q.text, q.why, *q.options])
        assert not re.search(rf"(?<![\d.])1 (more )?{word}s\b", joined), joined
        assert re.search(rf"(?<![\d.])1 (more )?{word}\b", joined), joined  # the case exists


def test_round_trip_vehicle_is_realistic():
    nouns = set()
    for q, f in _wide("speed_distance", "medium"):
        if "cyclist" in q.text:
            nouns.add("cyclist")
            assert f["u"] <= 40 and f["v"] <= 40, q.text
        else:
            nouns.add("car")
            assert q.text.startswith("A car "), q.text
    assert nouns == {"cyclist", "car"}


def test_ratio_terms_stay_small():
    ratio = re.compile(r"(\d+):(\d+)")
    for level in ("medium", "advanced"):
        for q, _f in _wide("ratio", level):
            for chunk in (q.text, *q.options):
                for x, y in ratio.findall(chunk):
                    assert int(x) <= 100 and int(y) <= 100, chunk
            if level == "medium":
                # every number the candidate meets (question, answer, options) stays <= 100
                for chunk in (q.text, *q.options):
                    assert all(Fraction(n) <= 100 for n in NUM.findall(chunk)), chunk


def test_time_work_explanation_uses_lowest_terms():
    frac = re.compile(r"(?<![\d/])(\d+)/(\d+)(?![\d/])")
    for level in ("medium", "advanced"):
        for q, f in _wide("time_work", level):
            per_day = Fraction(1, f["a"]) + Fraction(1, f["b"])
            if level == "medium":
                shown = f"{per_day.numerator}/{per_day.denominator}"
                assert f"= {shown} of the job a day" in q.why, q.why
            for x, y in frac.findall(q.why):
                assert Fraction(int(x), int(y)).denominator == int(y), q.why


def test_reverse_percentage_distractors_are_named_slips():
    for q, f in _wide("percentage", "advanced"):
        price, d = f["price"], f["discount"]
        slips = {
            Fraction(price * (100 + d), 100),  # added d% to the sale price
            Fraction(price * (100 - d), 100),  # took the discount off again
            Fraction(price * 100, 100 + d),  # divided by (1 + d%)
        }
        values = [_value(o) for i, o in enumerate(q.options) if i != q.answer]
        rounded = {Fraction(str(round(float(s), 2))) for s in slips}
        near = {_value(q.options[q.answer]) + k * 50 for k in (-2, -1, 1, 2, 3)}
        assert all(v in rounded or v in near for v in values), (q.options, f)
        assert price + d not in values

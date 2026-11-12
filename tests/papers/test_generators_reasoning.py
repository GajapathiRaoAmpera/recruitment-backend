"""Reasoning generators: every answer is re-derived here by an independent solver.

Solvers read the structured facts (via `reasoning._facts`, a test-only hook that replays the
same seeded rng) and recompute with separate code: brute force over orderings, Venn-region
models for syllogisms, `datetime` for calendars and an explicit family tree for relations.
"""

import datetime as dt
import itertools
import random
import re
import string

import pytest

from app.papers.generators import REASONING, GeneratedQuestion, generate_questions, reasoning
from app.papers.schemas import LEVELS, PoolQuestion

REQUIRED = {
    "number_series",
    "letter_series",
    "coding_decoding",
    "syllogism",
    "ordering",
    "calendar",
    "blood_relation",
}
SEEDS = [f"seed-{i}" for i in range(25)]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SYLLOGISM_OPTIONS = [
    "Only conclusion I follows",
    "Only conclusion II follows",
    "Both I and II follow",
    "Neither I nor II follows",
]
ABC = string.ascii_uppercase


# ---------------------------------------------------------------- number / letter series


def _next_number(f):
    t = f["terms"]
    kind = f["kind"]
    if kind == "arithmetic":
        diffs = {b - a for a, b in itertools.pairwise(t)}
        assert len(diffs) == 1
        return t[-1] + diffs.pop()
    if kind == "geometric":
        assert all(b % a == 0 for a, b in itertools.pairwise(t))
        ratios = {b // a for a, b in itertools.pairwise(t)}
        assert len(ratios) == 1
        return t[-1] * ratios.pop()
    if kind == "second_order":
        d = [b - a for a, b in itertools.pairwise(t)]
        dd = {b - a for a, b in itertools.pairwise(d)}
        assert len(dd) == 1 and 0 not in dd
        return t[-1] + d[-1] + dd.pop()
    assert kind == "interleaved"
    # odd and even positions are two separate arithmetic series
    nxt = len(t)
    same = t[nxt % 2 :: 2]
    steps = {b - a for a, b in itertools.pairwise(same)}
    assert len(steps) == 1
    return same[-1] + steps.pop()


def _next_letter(f):
    idx = [ABC.index(c) for c in f["letters"]]
    d = [(b - a) % 26 for a, b in itertools.pairwise(idx)]
    if f["kind"] in ("skip", "wrap"):
        assert len(set(d)) == 1
        return ABC[(idx[-1] + d[-1]) % 26]
    assert f["kind"] == "growing"
    grow = {b - a for a, b in itertools.pairwise(d)}
    assert len(grow) == 1
    return ABC[(idx[-1] + d[-1] + grow.pop()) % 26]


# ---------------------------------------------------------------- coding


def _encode(kind, shift, word):
    if kind == "shift":
        return "".join(ABC[(ABC.index(c) + shift) % 26] for c in word)
    if kind == "reverse_shift":
        return "".join(ABC[(ABC.index(c) + shift) % 26] for c in reversed(word))
    assert kind == "position_shift"
    return "".join(ABC[(ABC.index(c) + i) % 26] for i, c in enumerate(word, start=1))


# ---------------------------------------------------------------- syllogism (Venn models)


def _holds(form, x, y, inhabited):
    has = [r for r in range(1, 8) if inhabited >> r & 1]
    if form == "all":
        return not any(r >> x & 1 and not r >> y & 1 for r in has)
    if form == "some":
        return any(r >> x & 1 and r >> y & 1 for r in has)
    if form == "no":
        return not any(r >> x & 1 and r >> y & 1 for r in has)
    assert form == "some_not"
    return any(r >> x & 1 and not r >> y & 1 for r in has)


def _models():
    for inhabited in range(0, 256, 2):  # bit r set => Venn region r has members; r=0 ignored
        if all(any(inhabited >> r & 1 and r >> t & 1 for r in range(1, 8)) for t in range(3)):
            yield inhabited


def _syllogism_answer(f):
    terms = sorted({s[1] for s in f["premises"]} | {s[2] for s in f["premises"]})
    assert len(terms) == 3
    ix = {name: i for i, name in enumerate(terms)}

    def ok(stmt, m):
        return _holds(stmt[0], ix[stmt[1]], ix[stmt[2]], m)

    valid = [m for m in _models() if all(ok(p, m) for p in f["premises"])]
    assert valid
    one = all(ok(f["conclusions"][0], m) for m in valid)
    two = all(ok(f["conclusions"][1], m) for m in valid)
    if not one and not two:
        # never an "either I or II" case: that answer is not offered
        assert not all(ok(f["conclusions"][0], m) or ok(f["conclusions"][1], m) for m in valid)
    return {(True, False): 0, (False, True): 1, (True, True): 2, (False, False): 3}[(one, two)]


# ---------------------------------------------------------------- ordering (brute force)


def _clue_ok(clue, pos, n):
    kind = clue[0]
    if kind in ("taller", "left_of"):
        return pos[clue[1]] < pos[clue[2]]
    if kind == "immediately_left":
        return pos[clue[2]] - pos[clue[1]] == 1
    if kind == "next_to":
        return abs(pos[clue[1]] - pos[clue[2]]) == 1
    if kind == "end":
        return pos[clue[1]] in (0, n - 1)
    assert kind == "not_end"
    return pos[clue[1]] not in (0, n - 1)


def _ordering_solutions(f):
    people = f["people"]
    n = len(people)
    sols = []
    for perm in itertools.permutations(people):
        pos = {p: i for i, p in enumerate(perm)}
        if all(_clue_ok(c, pos, n) for c in f["clues"]):
            sols.append(perm)
    return sols


# ---------------------------------------------------------------- blood relations (family tree)


class _Node:
    def __init__(self, gender):
        self.gender = gender
        self.parents: tuple = ()
        self.spouse = None


def _marry(a, b):
    a.spouse, b.spouse = b, a


def _ensure_parents(u):
    if not u.parents:
        dad, mum = _Node("M"), _Node("F")
        _marry(dad, mum)
        u.parents = (dad, mum)


def _build_family(f):
    names, genders, hops = f["names"], f["genders"], f["hops"]
    nodes = {names[0]: _Node(genders[0])}
    for i, hop in enumerate(hops):
        u, g = nodes[names[i]], genders[i + 1]
        if hop == "parent":
            if u.spouse is None:
                _marry(u, _Node("F" if u.gender == "M" else "M"))
            v = _Node(g)
            v.parents = (u, u.spouse)
        elif hop == "child":
            if u.parents:
                v = next(p for p in u.parents if p.gender == g)
            else:
                v = _Node(g)
                partner = _Node("F" if g == "M" else "M")
                _marry(v, partner)
                u.parents = (v, partner)
        elif hop == "sibling":
            _ensure_parents(u)
            v = _Node(g)
            v.parents = u.parents
        else:
            assert hop == "spouse"
            if u.spouse is None:
                v = _Node(g)
                _marry(u, v)
            else:
                v = u.spouse
                assert v.gender == g
        assert v not in nodes.values()
        nodes[names[i + 1]] = v
    return nodes


def _siblings(a, b):
    return a is not b and bool(a.parents) and set(a.parents) == set(b.parents)


def _relation(x, y):
    m = x.gender == "M"

    def pick(male, female):
        return male if m else female

    if x.spouse is y:
        return pick("husband", "wife")
    if x in y.parents:
        return pick("father", "mother")
    if any(x in p.parents for p in y.parents):
        return pick("grandfather", "grandmother")
    if y in x.parents:
        return pick("son", "daughter")
    if any(y in p.parents for p in x.parents):
        return pick("grandson", "granddaughter")
    if _siblings(x, y):
        return pick("brother", "sister")
    for p in y.parents:
        if _siblings(x, p) or (x.spouse is not None and _siblings(x.spouse, p)):
            return pick("uncle", "aunt")
    if any(_siblings(p, y) for p in x.parents):
        return pick("nephew", "niece")
    if any(_siblings(p, q) for p in x.parents for q in y.parents):
        return "cousin"
    if y.spouse is not None and x in y.spouse.parents:
        return pick("father-in-law", "mother-in-law")
    if x.spouse is not None and y in x.spouse.parents:
        return pick("son-in-law", "daughter-in-law")
    if y.spouse is not None and _siblings(x, y.spouse):
        return pick("brother-in-law", "sister-in-law")
    if x.spouse is not None and _siblings(x.spouse, y):
        return pick("brother-in-law", "sister-in-law")
    return None


# ---------------------------------------------------------------- tests


def test_required_topics_present():
    assert REQUIRED <= set(REASONING)
    assert len(REASONING) >= 7


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("topic", sorted(REQUIRED))
def test_shape(topic, level):
    for seed in SEEDS:
        q = REASONING[topic](random.Random(seed), level)
        assert isinstance(q, GeneratedQuestion)
        assert q.topic == topic
        assert q.text.strip() and q.why.strip()
        assert len(q.options) == 4
        assert len({o.strip().casefold() for o in q.options}) == 4
        assert all(o.strip() for o in q.options)
        assert 0 <= q.answer < 4
        assert q.fixed_options is (topic == "syllogism")


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("topic", sorted(REQUIRED))
def test_same_seed_same_question(topic, level):
    assert REASONING[topic](random.Random("same"), level) == REASONING[topic](
        random.Random("same"), level
    )


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("topic", sorted(REQUIRED))
def test_seeds_give_variety(topic, level):
    texts = {REASONING[topic](random.Random(f"v{i}"), level).text for i in range(20)}
    assert len(texts) >= 10


def _cases(topic):
    for level in LEVELS:
        for seed in SEEDS:
            q = REASONING[topic](random.Random(seed), level)
            yield level, q, reasoning._facts(topic, random.Random(seed), level)


def test_number_series_answers():
    kinds = set()
    for _level, q, f in _cases("number_series"):
        kinds.add(f["kind"])
        assert ", ".join(str(t) for t in f["terms"]) in q.text
        assert q.options[q.answer] == str(_next_number(f)), (q.text, q.options, f)
    assert kinds == {"arithmetic", "geometric", "second_order", "interleaved"}


def test_letter_series_answers():
    kinds = set()
    for _level, q, f in _cases("letter_series"):
        kinds.add(f["kind"])
        assert ", ".join(f["letters"]) in q.text
        assert q.options[q.answer] == _next_letter(f), (q.text, q.options, f)
    assert kinds == {"skip", "growing", "wrap"}


def test_wrap_series_really_wraps():
    for level, _q, f in _cases("letter_series"):
        if f["kind"] == "wrap":
            idx = [ABC.index(c) for c in f["letters"]] + [ABC.index(_next_letter(f))]
            assert any(b < a for a, b in itertools.pairwise(idx)), f
            assert level == "advanced"


def test_coding_decoding_answers():
    for _level, q, f in _cases("coding_decoding"):
        assert _encode(f["kind"], f["shift"], f["example"]) == f["example_code"]
        for word in (f["example"], f["example_code"], f["word"]):
            assert word in q.text
        assert f["word"] != f["example"]
        assert q.options[q.answer] == _encode(f["kind"], f["shift"], f["word"])


def test_syllogism_answers_and_fixed_conventional_options():
    seen = set()
    for _level, q, f in _cases("syllogism"):
        assert q.fixed_options is True
        assert q.options == SYLLOGISM_OPTIONS
        expected = _syllogism_answer(f)
        seen.add(expected)
        assert q.answer == expected, (q.text, f)
    assert seen == {0, 1, 2, 3}


def test_ordering_has_exactly_one_solution_and_correct_answer():
    for _level, q, f in _cases("ordering"):
        sols = _ordering_solutions(f)
        assert len(sols) == 1, (q.text, sols)
        assert q.options[q.answer] == sols[0][f["ask"]]
        assert set(q.options) <= set(f["people"])
        for p in f["people"]:
            assert p in q.text


def test_ordering_raises_when_uniqueness_never_reached(monkeypatch):
    monkeypatch.setattr(reasoning, "_MAX_ORDERING_ATTEMPTS", 0)
    with pytest.raises(RuntimeError):
        REASONING["ordering"](random.Random("x"), "easy")


def test_calendar_matches_datetime():
    for _level, q, f in _cases("calendar"):
        if f["kind"] == "offset":
            base = dt.date(2024, 1, 1) + dt.timedelta(days=f["start"])  # 2024-01-01 is Monday
            assert DAY_NAMES[base.weekday()] in q.text
            want = (base + dt.timedelta(days=f["days"])).weekday()
        else:
            ref, target = dt.date(*f["ref"]), dt.date(*f["target"])
            assert ref.weekday() == f["ref_weekday"]
            assert DAY_NAMES[ref.weekday()] in q.text
            want = target.weekday()
        assert q.options[q.answer] == DAY_NAMES[want], (q.text, f)


def test_calendar_advanced_crosses_leap_day():
    for level, _q, f in _cases("calendar"):
        if level == "advanced":
            ref, target = dt.date(*f["ref"]), dt.date(*f["target"])
            years = range(ref.year, target.year + 1)
            leap_days = [
                dt.date(y, 2, 29) for y in years if (y % 4 == 0 and y % 100 != 0) or y % 400 == 0
            ]
            assert any(ref < d <= target for d in leap_days), f


def test_blood_relation_answers_match_family_tree():
    for _level, q, f in _cases("blood_relation"):
        nodes = _build_family(f)
        expected = _relation(nodes[f["names"][0]], nodes[f["names"][-1]])
        assert expected is not None, f
        assert q.options[q.answer] == expected, (q.text, q.options, f)
        assert f["names"][0] in q.text and f["names"][-1] in q.text


def test_difficulty_scales_for_blood_relations():
    for level, _q, f in _cases("blood_relation"):
        assert len(f["hops"]) == (3 if level == "advanced" else 2)


@pytest.mark.parametrize("level", LEVELS)
def test_generate_questions_reasoning(level):
    qs = generate_questions("reasoning", level, 21, "drive-1|reasoning")
    assert len(qs) == 21
    assert all(isinstance(q, PoolQuestion) for q in qs)
    assert len({q.id for q in qs}) == 21
    assert {q.topic for q in qs} == set(REASONING)
    for q in qs:
        assert q.section == "reasoning" and q.style == "reasoning" and q.level == level
        assert q.fixed_options is (q.topic == "syllogism")
    again = generate_questions("reasoning", level, 21, "drive-1|reasoning")
    assert [q.id for q in again] == [q.id for q in qs]


def test_generate_questions_survives_a_generator_that_gives_up(monkeypatch):
    def broken(rng, level):
        raise RuntimeError("could not build an ordering puzzle")

    monkeypatch.setitem(REASONING, "ordering", broken)
    qs = generate_questions("reasoning", "medium", 21, "drive-1|reasoning")
    assert len(qs) == 21
    assert {q.topic for q in qs} == set(REASONING) - {"ordering"}


def test_generate_questions_only_raises_value_error(monkeypatch):
    def broken(rng, level):
        raise RuntimeError("gave up")

    for name in list(REASONING):
        monkeypatch.setitem(REASONING, name, broken)
    with pytest.raises(ValueError):
        generate_questions("reasoning", "easy", 1, "s")


# ---------------------------------------------------------------- the wording candidates read
#
# The sentences below are rebuilt here from the hidden facts with the test's own templates, so
# a generator that prints a clue, premise, date or relation differently from the facts it
# solved (swapped terms, left/right, a shifted date, the wrong gender) fails.

MONTHS = [
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December",
]  # fmt: skip
ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth"]
WIDE_SEEDS = [f"wording-{i}" for i in range(60)]


def _wide_cases(topic):
    for level in LEVELS:
        for seed in WIDE_SEEDS:
            q = REASONING[topic](random.Random(seed), level)
            yield level, q, reasoning._facts(topic, random.Random(seed), level)


def _statement(s):
    form, x, y = s
    if form == "all":
        return f"All {x} are {y}."
    if form == "some":
        return f"Some {x} are {y}."
    if form == "no":
        return f"No {x} are {y}."
    assert form == "some_not"
    return f"Some {x} are not {y}."


def test_syllogism_text_states_exactly_the_solved_premises_and_conclusions():
    for _level, q, f in _wide_cases("syllogism"):
        expected = (
            "Statements: "
            + " ".join(_statement(p) for p in f["premises"])
            + f" Conclusions: I. {_statement(f['conclusions'][0])}"
            + f" II. {_statement(f['conclusions'][1])}"
        )
        assert q.text.startswith(expected), (q.text, f)


def _restated(conclusion, premises):
    return any(
        p[0] == conclusion[0] and {p[1], p[2]} == {conclusion[1], conclusion[2]} for p in premises
    )


def test_syllogism_restated_premises_are_rare():
    for level in LEVELS:
        hits = 0
        for i in range(200):
            f = reasoning._facts("syllogism", random.Random(f"r{i}"), level)
            hits += any(_restated(c, f["premises"]) for c in f["conclusions"])
        if level == "easy":
            assert hits <= 200 * 0.3, hits
        else:
            assert hits == 0, (level, hits)


def _clue_sentence(clue, seating):
    kind = clue[0]
    if kind == "taller":
        return f"{clue[1]} is taller than {clue[2]}."
    if kind == "left_of":
        return f"{clue[1]} sits somewhere to the left of {clue[2]}."
    if kind == "immediately_left":
        return f"{clue[1]} sits immediately to the left of {clue[2]}."
    if kind == "next_to":
        return f"{clue[1]} sits next to {clue[2]}."
    if kind == "end":
        return f"{clue[1]} sits at one of the ends."
    assert kind == "not_end"
    if seating:
        return f"{clue[1]} does not sit at either end."
    return f"{clue[1]} is neither the tallest nor the shortest."


def test_ordering_text_states_every_solved_clue_and_the_asked_position():
    for level, q, f in _wide_cases("ordering"):
        seating = level == "advanced"
        n, k = len(f["people"]), f["ask"]
        sentences = [_clue_sentence(c, seating) for c in f["clues"]]
        for s in sentences:
            assert s in q.text, (s, q.text)
        # nothing but the solved clues sits between the intro and the question
        if seating:
            question = f"Who sits {ORDINALS[k]} from the left?"
        elif k == 0:
            question = "Who is the tallest?"
        elif k == n - 1:
            question = "Who is the shortest?"
        else:
            question = f"Who is the {ORDINALS[k]} tallest?"
        assert q.text.endswith(" ".join([*sentences, question])), q.text


def _say_date(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def test_calendar_text_states_the_solved_dates():
    for _level, q, f in _wide_cases("calendar"):
        if f["kind"] == "offset":
            base = dt.date(2024, 1, 1) + dt.timedelta(days=f["start"])
            assert q.text.startswith(f"Today is {DAY_NAMES[base.weekday()]}."), q.text
            assert f" {f['days']} days from today?" in q.text, q.text
        else:
            ref, target = dt.date(*f["ref"]), dt.date(*f["target"])
            assert q.text.startswith(f"{_say_date(ref)} falls on a {DAY_NAMES[ref.weekday()]}."), (
                q.text
            )
            assert q.text.endswith(f"does {_say_date(target)} fall?"), q.text


RELATION_WORDS = {
    ("parent", "M"): "father",
    ("parent", "F"): "mother",
    ("child", "M"): "son",
    ("child", "F"): "daughter",
    ("sibling", "M"): "brother",
    ("sibling", "F"): "sister",
    ("spouse", "M"): "husband",
    ("spouse", "F"): "wife",
}


def test_blood_relation_text_states_each_solved_link():
    for _level, q, f in _wide_cases("blood_relation"):
        names, genders, hops = f["names"], f["genders"], f["hops"]
        sentences = [
            f"{names[i]} is the {RELATION_WORDS[(hop, genders[i])]} of {names[i + 1]}."
            for i, hop in enumerate(hops)
        ]
        expected = " ".join(sentences) + f" How is {names[0]} related to {names[-1]}?"
        assert q.text == expected, (q.text, f)


def test_series_and_coding_text_state_the_solved_facts():
    for _level, q, f in _wide_cases("number_series"):
        assert q.text == f"What comes next in the series: {', '.join(map(str, f['terms']))}, ?"
    for _level, q, f in _wide_cases("letter_series"):
        assert q.text == f"What comes next in the series: {', '.join(f['letters'])}, ?"
    for _level, q, f in _wide_cases("coding_decoding"):
        assert q.text == (
            f"In a certain code, {f['example']} is written as {f['example_code']}. "
            f"How is {f['word']} written in that code?"
        )


# ---------------------------------------------------------------- grammar


def test_no_broken_plurals_in_any_reasoning_text():
    bad = re.compile(r"(?<![\d.])1 (days|places|leap days|years|months)\b|\(s\)")
    for topic in REASONING:
        for _level, q, _f in _wide_cases(topic):
            for chunk in (q.text, q.why, *q.options):
                assert not bad.search(chunk), (topic, chunk)

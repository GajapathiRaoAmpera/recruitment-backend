"""Reasoning generators: the answer is computed by code from the puzzle's own facts.

Syllogisms are decided over every Venn-diagram model; ordering puzzles are regenerated until a
brute-force search finds exactly one arrangement; calendar answers come from day counting;
blood relations come from a fixed composition table of unambiguous chains.
"""

import itertools
import random
import string

from app.papers.generators.common import (
    Builder,
    Facts,
    GeneratedQuestion,
    Generator,
    place_answer,
    plural,
    public,
)
from app.papers.schemas import Level

ABC = string.ascii_uppercase
_ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth"]


def _q(
    topic: str, text: str, opts: tuple[list[str], int], why: str, fixed: bool = False
) -> GeneratedQuestion:
    options, answer = opts
    return GeneratedQuestion(
        topic=topic, text=text, options=options, answer=answer, why=why, fixed_options=fixed
    )


# ---------------------------------------------------------------------------- number series


def _number_series(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    if level == "easy":
        start, step = rng.randrange(2, 51), rng.randrange(2, 13)
        terms = [start + i * step for i in range(5)]
        ans = terms[-1] + step
        kind = "arithmetic"
        why = f"Each term adds {step}: {terms[-1]} + {step} = {ans}."
        slips = [ans + 1, ans - 1, ans + step]
    elif level == "medium" and rng.random() < 0.5:
        start, ratio = rng.randrange(1, 7), rng.choice([2, 3])
        terms = [start * ratio**i for i in range(5)]
        ans = terms[-1] * ratio
        kind = "geometric"
        why = f"Each term is multiplied by {ratio}: {terms[-1]} x {ratio} = {ans}."
        slips = [terms[-1] + (terms[-1] - terms[-2]), ans + ratio, terms[-1] * (ratio + 1)]
    elif level == "medium":
        start, first, grow = rng.randrange(1, 21), rng.randrange(1, 6), rng.randrange(1, 5)
        terms = [start]
        for i in range(4):
            terms.append(terms[-1] + first + i * grow)
        last_gap = first + 3 * grow
        ans = terms[-1] + last_gap + grow
        kind = "second_order"
        why = (
            f"The gaps grow by {grow} each time ({first}, {first + grow}, ...); the next gap is "
            f"{last_gap + grow}, so {terms[-1]} + {last_gap + grow} = {ans}."
        )
        slips = [terms[-1] + last_gap, ans + grow, ans - 1]
    else:
        a, b = rng.randrange(1, 30), rng.randrange(1, 30)
        p, q = rng.sample(range(2, 10), 2)
        odd = [a + i * p for i in range(4)]
        even = [b + i * q for i in range(3)]
        terms = [x for pair in itertools.zip_longest(odd, even) for x in pair if x is not None]
        ans = even[-1] + q  # 8th term continues the second series
        kind = "interleaved"
        why = (
            f"Two series alternate: {', '.join(map(str, odd))} (+{p}) and "
            f"{', '.join(map(str, even))} (+{q}). The next term belongs to the second: "
            f"{even[-1]} + {q} = {ans}."
        )
        slips = [odd[-1] + p, terms[-1] + q, ans + p]
    text = f"What comes next in the series: {', '.join(map(str, terms))}, ?"
    opts = place_answer(rng, str(ans), [str(s) for s in slips if s > 0] + _near(ans))
    return _q("number_series", text, opts, why), {"kind": kind, "terms": terms}


def _near(ans: int) -> list[str]:
    return [str(v) for v in (ans + 2, ans - 2, ans + 3, ans + 4) if v > 0]


# ---------------------------------------------------------------------------- letter series


def _letter_series(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    if level == "easy":
        step = rng.randrange(1, 5)
        start = rng.randrange(0, 26 - 5 * step)
        idx = [start + i * step for i in range(5)]
        nxt = idx[-1] + step
        kind = "skip"
        why = (
            f"Each letter moves {plural(step, 'place')} forward: "
            f"{ABC[idx[-1]]} + {step} = {ABC[nxt]}."
        )
        slips = [nxt + 1, nxt - 1, nxt + step]
    elif level == "medium":
        first = rng.randrange(1, 3)
        gaps = [first + i for i in range(5)]  # 4 shown gaps + the next one
        start = rng.randrange(0, 26 - sum(gaps))
        idx = [start]
        for g in gaps[:4]:
            idx.append(idx[-1] + g)
        nxt = idx[-1] + gaps[4]
        kind = "growing"
        why = (
            f"The gaps grow by one: {', '.join(map(str, gaps[:4]))}; the next gap is "
            f"{gaps[4]}, so {ABC[idx[-1]]} + {gaps[4]} = {ABC[nxt]}."
        )
        slips = [idx[-1] + gaps[3], nxt + 1, nxt - 1]
    else:
        step = rng.randrange(2, 6)
        start = rng.randrange(26 - 4 * step, 26)  # the series passes Z and wraps to A
        idx = [(start + i * step) % 26 for i in range(5)]
        nxt = (idx[-1] + step) % 26
        kind = "wrap"
        why = (
            f"Each letter moves {step} places forward, wrapping from Z back to A: "
            f"{ABC[idx[-1]]} + {step} = {ABC[nxt]}."
        )
        slips = [nxt + 1, nxt - 1, idx[-1] - step]
    letters = [ABC[i] for i in idx]
    text = f"What comes next in the series: {', '.join(letters)}, ?"
    extra = [nxt + 2, nxt - 2, nxt + 3]
    opts = place_answer(rng, ABC[nxt % 26], [ABC[s % 26] for s in (*slips, *extra)])
    return _q("letter_series", text, opts, why), {"kind": kind, "letters": letters}


# ---------------------------------------------------------------------------- coding-decoding

_WORDS = [
    "MILK", "BOOK", "LAMP", "TREE", "PLAN", "GOLD", "FISH", "RAIN", "STAR", "WIND", "CODE",
    "DESK", "LION", "ROAD", "SHIP", "BIRD", "CAKE", "COIN", "DOOR", "FARM", "HOME", "KITE",
    "PEN", "CUP", "SUN", "MAP", "BAG", "JAR", "WATER", "PLANT", "CHAIR", "TABLE", "MOUSE",
]  # fmt: skip


def _shift(word: str, by: int) -> str:
    return "".join(ABC[(ABC.index(c) + by) % 26] for c in word)


def _by_position(word: str, offset: int = 1) -> str:
    return "".join(ABC[(ABC.index(c) + i) % 26] for i, c in enumerate(word, start=offset))


def _coding(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    example, word = rng.sample(_WORDS, 2)
    if level == "easy":
        k = rng.choice([-2, -1, 1, 2, 3])
        kind = "shift"

        def enc(w: str) -> str:
            return _shift(w, k)

        rule = f"each letter moves {plural(abs(k), 'place')} {'forward' if k > 0 else 'back'}"
        over = k + 1 if k > 0 else k - 1  # moved one place too far
        slips = [_shift(word, over), _shift(word, -k), _shift(word[::-1], k)]
    elif level == "medium":
        k = rng.choice([-1, 1, 2])
        kind = "reverse_shift"

        def enc(w: str) -> str:
            return _shift(w[::-1], k)

        rule = (
            f"the word is reversed and each letter moves {plural(abs(k), 'place')} "
            f"{'forward' if k > 0 else 'back'}"
        )
        slips = [_shift(word, k), _shift(word[::-1], -k), _shift(word[::-1], k + 1)]
    else:
        k = 0
        kind = "position_shift"

        def enc(w: str) -> str:
            return _by_position(w)

        rule = "the n-th letter moves n places forward (1st +1, 2nd +2, ...), wrapping after Z"
        slips = [_shift(word, 1), _by_position(word, 0), _by_position(word[::-1])]
    example_code, ans = enc(example), enc(word)
    text = (
        f"In a certain code, {example} is written as {example_code}. "
        f"How is {word} written in that code?"
    )
    why = f"From {example} -> {example_code}: {rule}. Applying it to {word} gives {ans}."
    extra = [_shift(ans, 1), _shift(ans, -1), ans[::-1]]
    opts = place_answer(rng, ans, [*slips, *extra])
    return _q("coding_decoding", text, opts, why), {
        "kind": kind,
        "shift": k,
        "example": example,
        "example_code": example_code,
        "word": word,
    }


# ---------------------------------------------------------------------------- syllogism

SYLLOGISM_OPTIONS = [
    "Only conclusion I follows",
    "Only conclusion II follows",
    "Both I and II follow",
    "Neither I nor II follows",
]
_TERMS = [
    "pens", "books", "cups", "chairs", "lamps", "boxes", "roses", "birds", "coins", "maps",
    "keys", "bags", "cats", "trees", "doors", "clocks", "stones", "shirts", "kites", "bells",
]  # fmt: skip
_FORMS_BY_LEVEL: dict[Level, tuple[list[str], list[str]]] = {
    # (premise forms, conclusion forms)
    "easy": (["all", "all", "some"], ["all", "some"]),
    "medium": (["all", "some", "no"], ["all", "some", "no"]),
    "advanced": (["all", "some", "no", "some_not"], ["all", "some", "no", "some_not"]),
}
Statement = tuple[str, str, str]  # (form, subject, predicate)


def _say(s: Statement) -> str:
    form, x, y = s
    return {
        "all": f"All {x} are {y}.",
        "some": f"Some {x} are {y}.",
        "no": f"No {x} are {y}.",
        "some_not": f"Some {x} are not {y}.",
    }[form]


# Venn regions for three terms: a region is the set of term indexes it lies inside.
_REGIONS = [frozenset(t for t in range(3) if r >> t & 1) for r in range(1, 8)]
_VENN_MODELS = [
    frozenset(r for r, on in zip(_REGIONS, flags, strict=True) if on)
    for flags in itertools.product([False, True], repeat=7)
]
# every term names at least one thing (the usual convention in these questions)
_VENN_MODELS = [m for m in _VENN_MODELS if all(any(t in r for r in m) for t in range(3))]


def _true_in(s: Statement, model: frozenset[frozenset[int]], ix: dict[str, int]) -> bool:
    form, x, y = s[0], ix[s[1]], ix[s[2]]
    both = any(x in r and y in r for r in model)
    x_only = any(x in r and y not in r for r in model)
    return {"all": not x_only, "some": both, "no": not both, "some_not": x_only}[form]


def _judge(
    premises: list[Statement], conclusions: list[Statement], ix: dict[str, int]
) -> int | None:
    """Fixed-option index, or None for an "either I or II" case (not offered)."""
    models = [m for m in _VENN_MODELS if all(_true_in(p, m, ix) for p in premises)]
    if not models:
        return None
    one = all(_true_in(conclusions[0], m, ix) for m in models)
    two = all(_true_in(conclusions[1], m, ix) for m in models)
    if not one and not two:
        if all(_true_in(conclusions[0], m, ix) or _true_in(conclusions[1], m, ix) for m in models):
            return None
    return {(True, False): 0, (False, True): 1, (True, True): 2, (False, False): 3}[(one, two)]


_EASY_RESTATED_SHARE = 0.25


def _restates(conclusion: Statement, premises: list[Statement]) -> bool:
    form, x, y = conclusion
    return any(p[0] == form and {p[1], p[2]} == {x, y} for p in premises)


def _syllogism(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    premise_forms, conclusion_forms = _FORMS_BY_LEVEL[level]
    target = rng.randrange(4)  # spread answers evenly over the four options
    # A conclusion that only restates a premise (same form, possibly reversed) is a giveaway:
    # allowed in about 1 easy question in 4, never at medium or advanced.
    allow_restated = level == "easy" and rng.random() < _EASY_RESTATED_SHARE
    chosen: tuple[list[Statement], list[Statement], int] | None = None
    for _ in range(400):
        a, b, c = rng.sample(_TERMS, 3)
        ix = {a: 0, b: 1, c: 2}
        p1 = (rng.choice(premise_forms), *rng.sample([a, b], 2))
        p2 = (rng.choice(premise_forms), *rng.sample([b, c], 2))
        pairs = [(a, c), (c, a), (a, b), (b, a), (b, c), (c, b), (a, c), (c, a)]
        c1 = (rng.choice(conclusion_forms), *rng.choice(pairs))
        c2 = (rng.choice(conclusion_forms), *rng.choice(pairs))
        premises, conclusions = [p1, p2], [c1, c2]
        if c1 == c2 or c1 in premises or c2 in premises:
            continue
        if not allow_restated and any(_restates(k, premises) for k in conclusions):
            continue
        answer = _judge(premises, conclusions, ix)
        if answer is None:
            continue
        if chosen is None or answer == target:
            chosen = (premises, conclusions, answer)
        if answer == target:
            break
    if chosen is None:
        raise RuntimeError("could not build a syllogism")
    premises, conclusions, answer = chosen
    text = (
        "Statements: "
        + " ".join(_say(p) for p in premises)
        + " Conclusions: I. "
        + _say(conclusions[0])
        + " II. "
        + _say(conclusions[1])
        + " Which of the conclusions follow from the statements?"
    )
    verdict = ["follows" if answer in (0, 2) else "does not follow"]
    verdict.append("follows" if answer in (1, 2) else "does not follow")
    why = (
        f"Draw every Venn diagram allowed by the statements. Conclusion I {verdict[0]} "
        f"(it {'holds in every' if answer in (0, 2) else 'fails in at least one'} diagram); "
        f"conclusion II {verdict[1]}. Answer: {SYLLOGISM_OPTIONS[answer]}."
    )
    opts = (list(SYLLOGISM_OPTIONS), answer)
    return _q("syllogism", text, opts, why, fixed=True), {
        "premises": premises,
        "conclusions": conclusions,
    }


# ---------------------------------------------------------------------------- ordering

_PEOPLE = [
    "Asha", "Bala", "Chitra", "Dev", "Esha", "Farid", "Gita", "Hari", "Indu", "Jai", "Kiran",
    "Lata", "Mohan", "Nila", "Omar", "Priya", "Ravi", "Sara",
]  # fmt: skip
_MAX_ORDERING_ATTEMPTS = 50
Clue = tuple[str, ...]


def _clue_holds(clue: Clue, pos: dict[str, int], n: int) -> bool:
    kind = clue[0]
    if kind in ("taller", "left_of"):
        return pos[clue[1]] < pos[clue[2]]
    if kind == "immediately_left":
        return pos[clue[1]] + 1 == pos[clue[2]]
    if kind == "next_to":
        return abs(pos[clue[1]] - pos[clue[2]]) == 1
    if kind == "end":
        return pos[clue[1]] in (0, n - 1)
    return pos[clue[1]] not in (0, n - 1)  # not_end


def _count_orders(people: list[str], clues: list[Clue]) -> int:
    n = len(people)
    count = 0
    for perm in itertools.permutations(people):
        pos = {p: i for i, p in enumerate(perm)}
        if all(_clue_holds(c, pos, n) for c in clues):
            count += 1
    return count


def _true_clues(order: list[str], kinds: list[str]) -> list[Clue]:
    n = len(order)
    out: list[Clue] = []
    for i, j in itertools.permutations(range(n), 2):
        x, y = order[i], order[j]
        if i < j and "taller" in kinds:
            out.append(("taller", x, y))
        if i < j and "left_of" in kinds:
            out.append(("left_of", x, y))
        if j == i + 1 and "immediately_left" in kinds:
            out.append(("immediately_left", x, y))
        if i < j and j == i + 1 and "next_to" in kinds:
            out.append(("next_to", x, y))
    for i, x in enumerate(order):
        if "end" in kinds and i in (0, n - 1):
            out.append(("end", x))
        if "not_end" in kinds and i not in (0, n - 1):
            out.append(("not_end", x))
    return out


def _say_clue(clue: Clue, seating: bool) -> str:
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
    if seating:
        return f"{clue[1]} does not sit at either end."
    return f"{clue[1]} is neither the tallest nor the shortest."


_ORDERING_SPEC: dict[Level, tuple[int, list[str], int]] = {
    # people, clue kinds, most clues allowed
    "easy": (4, ["taller"], 5),
    "medium": (5, ["taller", "not_end"], 7),
    "advanced": (5, ["immediately_left", "next_to", "left_of", "end", "not_end"], 6),
}


def _ordering(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    n, kinds, max_clues = _ORDERING_SPEC[level]
    seating = level == "advanced"
    for _ in range(_MAX_ORDERING_ATTEMPTS):
        order = rng.sample(_PEOPLE, n)
        candidates = _true_clues(order, kinds)
        rng.shuffle(candidates)
        clues: list[Clue] = []
        remaining = _count_orders(order, clues)
        for clue in candidates:
            if remaining == 1 or len(clues) >= max_clues:
                break
            narrowed = _count_orders(order, [*clues, clue])
            if narrowed < remaining:
                clues.append(clue)
                remaining = narrowed
        if remaining == 1:
            break
    else:
        raise RuntimeError("could not build an ordering puzzle with exactly one solution")

    if level == "easy":
        ask = rng.choice([0, n - 1])
    else:
        ask = rng.randrange(n)
    people = sorted(order)
    if seating:
        intro = f"{', '.join(people)} sit in a row facing north."
        question = f"Who sits {_ORDINALS[ask]} from the left?"
        layout = "left to right"
    else:
        intro = f"{', '.join(people)} have different heights."
        if ask == 0:
            question = "Who is the tallest?"
        elif ask == n - 1:
            question = "Who is the shortest?"
        else:
            question = f"Who is the {_ORDINALS[ask]} tallest?"
        layout = "tallest to shortest"
    text = " ".join([intro, *(_say_clue(c, seating) for c in clues), question])
    ans = order[ask]
    neighbours = [order[i] for i in (ask - 1, ask + 1) if 0 <= i < n]
    others = [p for p in order if p != ans and p not in neighbours]
    rng.shuffle(others)
    opts = place_answer(rng, ans, [*neighbours, *others])
    why = (
        f"The only arrangement ({layout}) that satisfies every clue is "
        f"{', '.join(order)}, so the answer is {ans}."
    )
    return _q("ordering", text, opts, why), {"people": people, "clues": clues, "ask": ask}


# ---------------------------------------------------------------------------- calendar

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = [
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December",
]  # fmt: skip
_MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _day_number(year: int, month: int, day: int) -> int:
    """Days since 1 January of year 1 (which was a Monday), counting by hand."""
    y = year - 1
    days = 365 * y + y // 4 - y // 100 + y // 400
    days += sum(_MONTH_DAYS[: month - 1]) + (1 if month > 2 and _is_leap(year) else 0)
    return days + day - 1


def _weekday(year: int, month: int, day: int) -> int:
    return _day_number(year, month, day) % 7  # 0 = Monday


def _say_date(d: tuple[int, int, int]) -> str:
    return f"{d[2]} {_MONTHS[d[1] - 1]} {d[0]}"


def _leap_days_between(ref: tuple[int, int, int], target: tuple[int, int, int]) -> int:
    lo, hi = _day_number(*ref), _day_number(*target)
    return sum(
        1 for y in range(ref[0], target[0] + 1) if _is_leap(y) and lo < _day_number(y, 2, 29) <= hi
    )


def _calendar(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    if level == "easy":
        start, days = rng.randrange(7), rng.randrange(9, 81)
        ans = (start + days) % 7
        text = (
            f"Today is {DAY_NAMES[start]}. What day of the week will it be {days} days from today?"
        )
        why = (
            f"{days} = 7 x {days // 7} + {days % 7}; whole weeks change nothing, so move "
            f"{plural(days % 7, 'day')} on from {DAY_NAMES[start]}: {DAY_NAMES[ans]}."
        )
        slips = [ans + 1, ans - 1, start - days]
        facts: Facts = {"kind": "offset", "start": start, "days": days}
    else:
        while True:
            year = rng.randrange(1995, 2031)
            ref = (year, rng.randrange(1, 10), rng.randrange(1, 29))
            if level == "medium":
                target = (year, rng.randrange(ref[1] + 1, 13), rng.randrange(1, 29))
            else:
                target = (year + rng.randrange(1, 5), rng.randrange(1, 13), rng.randrange(1, 29))
            leaps = _leap_days_between(ref, target)
            if level == "medium" or leaps:
                break
        ref_day = _weekday(*ref)
        gap = _day_number(*target) - _day_number(*ref)
        ans = (ref_day + gap) % 7
        text = (
            f"{_say_date(ref)} falls on a {DAY_NAMES[ref_day]}. On which day of the week "
            f"does {_say_date(target)} fall?"
        )
        why = (
            f"There are {plural(gap, 'day')} from {_say_date(ref)} to {_say_date(target)}"
            + (f" (including {plural(leaps, 'leap day')})" if leaps else "")
            + f". {gap} mod 7 = {gap % 7}, so {DAY_NAMES[ref_day]} + {gap % 7} = "
            f"{DAY_NAMES[ans]}."
        )
        if level == "medium":
            # slip: treating every month as 30 days
            months = target[1] - ref[1]
            slips = [ref_day + months * 30 + target[2] - ref[2], ans + 1, ans - 1]
        else:
            slips = [ans - leaps, ans + 1, ans - 1]
        facts = {"kind": "dates", "ref": list(ref), "ref_weekday": ref_day, "target": list(target)}
    fill = [ans + 2, ans - 2, ans + 3]
    opts = place_answer(rng, DAY_NAMES[ans], [DAY_NAMES[s % 7] for s in (*slips, *fill)])
    return _q("calendar", text, opts, why), facts


# ---------------------------------------------------------------------------- blood relations

_MALE = ["Arjun", "Vikram", "Rahul", "Sanjay", "Karan", "Nikhil", "Rohan", "Imran", "Tarun"]
_FEMALE = ["Meera", "Anita", "Kavya", "Pooja", "Sneha", "Divya", "Fatima", "Rekha", "Leela"]
_HOP_WORDS = {
    "parent": ("father", "mother"),
    "child": ("son", "daughter"),
    "sibling": ("brother", "sister"),
    "spouse": ("husband", "wife"),
}
# "X is the <hop1> of A. A is the <hop2> of Y ..." -> X's relation to Y (male, female).
# Only chains that cannot be read two ways (e.g. sibling-of-sibling could be X again) are used.
_CHAINS: dict[tuple[str, ...], tuple[str, str]] = {
    ("parent", "parent"): ("grandfather", "grandmother"),
    ("child", "child"): ("grandson", "granddaughter"),
    ("parent", "sibling"): ("father", "mother"),
    ("sibling", "child"): ("son", "daughter"),
    ("sibling", "parent"): ("uncle", "aunt"),
    ("child", "sibling"): ("nephew", "niece"),
    ("spouse", "parent"): ("father", "mother"),
    ("child", "spouse"): ("son", "daughter"),
    ("parent", "spouse"): ("father-in-law", "mother-in-law"),
    ("spouse", "child"): ("son-in-law", "daughter-in-law"),
    ("spouse", "sibling"): ("brother-in-law", "sister-in-law"),
    ("sibling", "spouse"): ("brother-in-law", "sister-in-law"),
    ("parent", "parent", "sibling"): ("grandfather", "grandmother"),
    ("child", "sibling", "parent"): ("cousin", "cousin"),
    ("sibling", "parent", "sibling"): ("uncle", "aunt"),
    ("spouse", "parent", "parent"): ("grandfather", "grandmother"),
    ("child", "child", "spouse"): ("grandson", "granddaughter"),
    ("spouse", "sibling", "parent"): ("uncle", "aunt"),
    ("child", "spouse", "parent"): ("brother", "sister"),
    ("sibling", "child", "child"): ("grandson", "granddaughter"),
}
_BLOOD = {"parent", "child", "sibling"}
_CHAINS_BY_LEVEL: dict[Level, list[tuple[str, ...]]] = {
    "easy": [c for c in _CHAINS if len(c) == 2 and set(c) <= _BLOOD],
    "medium": [c for c in _CHAINS if len(c) == 2 and "spouse" in c],
    "advanced": [c for c in _CHAINS if len(c) == 3],
}
_VOCAB = {
    "M": [
        "father", "son", "brother", "uncle", "nephew", "grandfather", "grandson", "husband",
        "father-in-law", "son-in-law", "brother-in-law", "cousin",
    ],
    "F": [
        "mother", "daughter", "sister", "aunt", "niece", "grandmother", "granddaughter", "wife",
        "mother-in-law", "daughter-in-law", "sister-in-law", "cousin",
    ],
}  # fmt: skip


def _blood_relation(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    hops = rng.choice(_CHAINS_BY_LEVEL[level])
    genders = [rng.choice("MF")]
    for hop in hops:
        if hop == "spouse":
            genders.append("F" if genders[-1] == "M" else "M")
        else:
            genders.append(rng.choice("MF"))
    males = rng.sample(_MALE, len(genders))
    females = rng.sample(_FEMALE, len(genders))
    names = [males[i] if g == "M" else females[i] for i, g in enumerate(genders)]
    statements = [
        f"{names[i]} is the {_HOP_WORDS[hop][genders[i] == 'F']} of {names[i + 1]}."
        for i, hop in enumerate(hops)
    ]
    ans = _CHAINS[hops][genders[0] == "F"]
    text = " ".join(statements) + f" How is {names[0]} related to {names[-1]}?"
    why = (
        " ".join(statements)
        + f" Following the chain from {names[0]} to {names[-1]}, {names[0]} is "
        f"{names[-1]}'s {ans}."
    )
    first_hop = _HOP_WORDS[hops[0]][genders[0] == "F"]  # slip: answering the first link only
    pool = [w for w in _VOCAB[genders[0]] if w not in (ans, first_hop)]
    rng.shuffle(pool)
    opts = place_answer(rng, ans, [first_hop, *pool])
    return _q("blood_relation", text, opts, why), {
        "names": names,
        "genders": genders,
        "hops": list(hops),
    }


# ---------------------------------------------------------------------------- registry

_BUILDERS: dict[str, Builder] = {
    "number_series": _number_series,
    "letter_series": _letter_series,
    "coding_decoding": _coding,
    "syllogism": _syllogism,
    "ordering": _ordering,
    "calendar": _calendar,
    "blood_relation": _blood_relation,
}


REASONING: dict[str, Generator] = {name: public(b) for name, b in _BUILDERS.items()}


def _facts(topic: str, rng: random.Random, level: Level) -> Facts:
    """Test hook: the facts behind the question the same rng state would generate."""
    return _BUILDERS[topic](rng, level)[1]

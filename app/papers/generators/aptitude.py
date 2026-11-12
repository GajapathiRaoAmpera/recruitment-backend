"""Aptitude generators: the answer is computed from the generator's own numbers.

Every topic builder returns the question plus the structured facts it used. Only the
`GeneratedQuestion` reaches callers; `_facts` exists so tests can re-derive the answer with an
independent solver. Distractors are the classic slips for each topic (inverted ratio, forgotten
unit conversion, off-by-one count, ...), never random numbers.

Difficulty: easy = one step with round numbers, medium = two steps, advanced = a reverse step,
a unit conversion or an edge case.
"""

import random
from collections.abc import Callable, Iterable
from fractions import Fraction
from math import gcd

from app.papers.generators.common import (
    Builder,
    Facts,
    GeneratedQuestion,
    Generator,
    fmt_frac,
    fmt_num,
    place_answer,
    plural,
    public,
)
from app.papers.schemas import Level


def _numeric_options(
    rng: random.Random,
    answer: Fraction | int,
    slips: Iterable[Fraction | int],
    step: Fraction | int,
    unit: Callable[[str], str] = lambda s: s,
) -> tuple[list[str], int]:
    """Slips first; near misses (answer +/- step) only fill any gap. All values stay positive."""
    answer = Fraction(answer)
    near = [answer + step, answer - step, answer + 2 * step, answer - 2 * step, answer + 3 * step]
    # keep a value only if it is still positive after rounding for display
    shown = [fmt_num(v) for v in (*slips, *near)]
    candidates = [unit(s) for s in shown if Fraction(s) > 0]
    return place_answer(rng, unit(fmt_num(answer)), candidates)


def _rupees(s: str) -> str:
    return f"Rs {s}"


def _pct(s: str) -> str:
    return f"{s}%"


def _unit(singular: str, plural_form: str | None = None) -> Callable[[str], str]:
    return lambda s: plural(s, singular, plural_form)


def _q(topic: str, text: str, opts: tuple[list[str], int], why: str) -> GeneratedQuestion:
    options, answer = opts
    return GeneratedQuestion(topic=topic, text=text, options=options, answer=answer, why=why)


# ---------------------------------------------------------------------------- percentage

_ITEMS = ["jacket", "phone", "watch", "bicycle", "lamp", "chair", "backpack", "printer"]


def _percentage(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    if level == "easy":
        p = rng.choice([5, 10, 20, 25, 40, 50, 75])
        n = rng.randrange(2, 60) * 20
        ans = Fraction(n * p, 100)
        text = f"What is {p}% of {n}?"
        why = f"{p}% of {n} = {n} x {p}/100 = {fmt_num(ans)}."
        slips = [n - ans, ans * 10, ans / 10]
        return _q("percentage", text, _numeric_options(rng, ans, slips, 5), why), {"p": p, "n": n}
    if level == "medium":
        n = rng.randrange(2, 50) * 100
        up = rng.choice([10, 20, 25, 30, 40, 50])
        down = rng.choice([10, 20, 25, 30, 40, 50])
        after_up = Fraction(n * (100 + up), 100)
        ans = after_up * (100 - down) / 100
        text = (
            f"A price of Rs {n} is increased by {up}% and the new price is then decreased "
            f"by {down}%. What is the final price?"
        )
        why = (
            f"After +{up}%: {n} x {100 + up}/100 = {fmt_num(after_up)}. "
            f"After -{down}%: {fmt_num(after_up)} x {100 - down}/100 = {fmt_num(ans)}."
        )
        slips = [Fraction(n * (100 + up - down), 100), after_up, Fraction(n * (100 - down), 100)]
        opts = _numeric_options(rng, ans, slips, 10, _rupees)
        return _q("percentage", text, opts, why), {"n": n, "up": up, "down": down}
    item = rng.choice(_ITEMS)
    discount = rng.choice([10, 15, 20, 25, 30, 40])
    original = rng.randrange(4, 100) * 100
    price = original * (100 - discount) // 100
    text = (
        f"After a discount of {discount}%, a {item} sells for Rs {price}. "
        f"What was its price before the discount?"
    )
    why = (
        f"The sale price is {100 - discount}% of the original, so the original is "
        f"{price} x 100/{100 - discount} = {original}. (Adding {discount}% to {price} is wrong: "
        f"the discount was taken on the original price.)"
    )
    slips = [
        Fraction(price * (100 + discount), 100),
        Fraction(price * (100 - discount), 100),
        Fraction(price * 100, 100 + discount),  # divided by (1 + d%) instead of (1 - d%)
    ]
    opts = _numeric_options(rng, original, slips, 50, _rupees)
    return _q("percentage", text, opts, why), {"price": price, "discount": discount}


# ---------------------------------------------------------------------------- profit & loss


def _profit_loss(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    if level == "easy":
        cost = rng.randrange(5, 100) * 20
        p = rng.choice([5, 10, 15, 20, 25, 30, 40, 50])
        profit = Fraction(cost * p, 100)
        ans = cost + profit
        text = (
            f"An article is bought for Rs {cost} and sold at a profit of {p}%. "
            f"What is the selling price?"
        )
        why = f"Profit = {p}% of {cost} = {fmt_num(profit)}; selling price = {fmt_num(ans)}."
        slips = [cost - profit, profit, cost + p]
        opts = _numeric_options(rng, ans, slips, 10, _rupees)
        return _q("profit_loss", text, opts, why), {"cost": cost, "profit_pct": p}
    if level == "medium":
        cost = rng.randrange(5, 60) * 20
        p = rng.choice([5, 10, 15, 20, 25, 30, 40, 50])
        sell = cost * (100 + p) // 100
        profit = sell - cost
        text = f"A trader buys a crate for Rs {cost} and sells it for Rs {sell}. Find the profit %."
        why = f"Profit = {sell} - {cost} = {profit}; profit % = {profit}/{cost} x 100 = {p}%."
        slips = [Fraction(profit * 100, sell), profit]
        opts = _numeric_options(rng, p, slips, 5, _pct)
        return _q("profit_loss", text, opts, why), {"cost": cost, "sell": sell}
    while True:
        cost = rng.randrange(5, 50) * 100
        markup = rng.choice([20, 25, 30, 40, 50, 60])
        discount = rng.choice([10, 15, 20, 25])
        if (100 + markup) * (100 - discount) > 10_000:
            break
    ans = Fraction((100 + markup) * (100 - discount) - 10_000, 100)
    text = (
        f"A shopkeeper marks an article costing Rs {cost} at {markup}% above cost and then "
        f"allows a discount of {discount}% on the marked price. What is the profit %?"
    )
    why = (
        f"Selling price = cost x {100 + markup}/100 x {100 - discount}/100, so profit % = "
        f"{(100 + markup) * (100 - discount) / 100:g} - 100 = {fmt_num(ans)}%. "
        f"Simply subtracting {markup} - {discount} ignores that the discount is on the "
        f"higher price."
    )
    slips = [markup - discount, markup, discount]
    opts = _numeric_options(rng, ans, slips, 5, _pct)
    return _q("profit_loss", text, opts, why), {
        "cost": cost,
        "markup": markup,
        "discount": discount,
    }


# ---------------------------------------------------------------------------- ratio

_COPRIME = [(a, b) for a in range(1, 10) for b in range(1, 10) if a != b and gcd(a, b) == 1]
_RATIO_MAX = 100


def _ratio(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    if level == "easy":
        a, b = rng.choice(_COPRIME)
        part = rng.randrange(5, 60)
        total = part * (a + b)
        ans = a * part
        text = f"Divide Rs {total} between P and Q in the ratio {a}:{b}. What is P's share?"
        why = f"One part = {total}/({a}+{b}) = {part}; P gets {a} parts = {ans}."
        slips = [b * part, part, (a + 1) * part]
        opts = _numeric_options(rng, ans, slips, part, _rupees)
        return _q("ratio", text, opts, why), {"total": total, "a": a, "b": b}
    if level == "medium":
        while True:  # keep every number (and so every ratio term) at most 100
            a, b = rng.choice([(x, y) for x, y in _COPRIME if x < y])
            x = rng.randrange(3, 30)
            add = rng.randrange(2, 20)
            if b * x + add <= _RATIO_MAX:
                break
        g = gcd(a * x + add, b * x + add)
        c, d = (a * x + add) // g, (b * x + add) // g
        ans = a * x
        text = (
            f"Two numbers are in the ratio {a}:{b}. If {add} is added to each, the ratio "
            f"becomes {c}:{d}. What is the smaller number?"
        )
        why = (
            f"Let the numbers be {a}k and {b}k. ({a}k + {add}) x {d} = ({b}k + {add}) x {c} "
            f"gives k = {x}, so the smaller number is {a} x {x} = {ans}."
        )
        slips = [b * x, x, a * x + add]
        opts = _numeric_options(rng, ans, slips, 1)
        return _q("ratio", text, opts, why), {"a": a, "b": b, "add": add, "c": c, "d": d}
    while True:
        metres = rng.randrange(2, 30)
        a, b = rng.choice(_COPRIME)
        if (metres * 100 * min(a, b)) % (a + b) == 0:
            break
    short_cm = Fraction(metres * 100 * min(a, b), a + b)
    long_cm = metres * 100 - short_cm
    text = (
        f"A rope {metres} metres long is cut into two pieces in the ratio {a}:{b}. "
        f"How long is the shorter piece, in centimetres?"
    )
    why = (
        f"{metres} m = {metres * 100} cm. Shorter piece = {metres * 100} x "
        f"{min(a, b)}/{a + b} = {fmt_num(short_cm)} cm."
    )
    slips = [short_cm / 100, long_cm, short_cm * 10]
    opts = _numeric_options(rng, short_cm, slips, 10, _unit("cm", "cm"))
    return _q("ratio", text, opts, why), {"metres": metres, "a": a, "b": b}


# ---------------------------------------------------------------------------- time & work

_TOGETHER = [(a, b) for a in range(2, 61) for b in range(a + 1, 61) if (a * b) % (a + b) == 0]


def _time_work(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    if level == "easy":
        while True:
            w1, d1, w2 = rng.randrange(2, 13), rng.randrange(2, 31), rng.randrange(2, 25)
            if w2 != w1 and (w1 * d1) % w2 == 0:
                break
        ans = w1 * d1 // w2
        text = (
            f"{w1} workers can build a wall in {d1} days. How many days would {w2} workers "
            f"take at the same rate?"
        )
        why = (
            f"Total work = {w1} x {d1} = {w1 * d1} worker-days; "
            f"{w1 * d1}/{w2} = {plural(ans, 'day')}."
        )
        slips = [Fraction(d1 * w2, w1), d1, ans + 1]
        opts = _numeric_options(rng, ans, slips, 1, _unit("day"))
        return _q("time_work", text, opts, why), {"workers": w1, "days": d1, "new_workers": w2}
    if level == "medium":
        a, b = rng.choice(_TOGETHER)
        ans = a * b // (a + b)
        text = (
            f"A can finish a job in {a} days and B in {b} days. Working together, how many "
            f"days will they take?"
        )
        per_day = fmt_frac(Fraction(1, a) + Fraction(1, b))
        why = f"Together they do 1/{a} + 1/{b} = {per_day} of the job a day: {plural(ans, 'day')}."
        slips = [Fraction(a + b, 2), a + b, b - a]
        opts = _numeric_options(rng, ans, slips, 1, _unit("day"))
        return _q("time_work", text, opts, why), {"a": a, "b": b}
    while True:
        a, b = rng.randrange(4, 41), rng.randrange(4, 41)
        k = rng.randrange(1, 10)
        if a == b or (k * b) % a:
            continue
        rest = b - k * b // a - k
        if rest > 0:
            break
    total = k + rest
    done = Fraction(k, a) + Fraction(k, b)
    text = (
        f"A can do a job in {a} days and B in {b} days. They work together for "
        f"{plural(k, 'day')}, "
        f"then A leaves and B finishes the rest. How many days does the whole job take?"
    )
    why = (
        f"In {plural(k, 'day')} they finish {fmt_frac(done)} of the job. B needs "
        f"{plural(rest, 'more day', 'more days')} for the rest, so the job takes {k} + {rest} = "
        f"{plural(total, 'day')}."
    )
    slips = [rest, b - k, k + b]
    opts = _numeric_options(rng, total, slips, 1, _unit("day"))
    return _q("time_work", text, opts, why), {"a": a, "b": b, "together_days": k}


# ---------------------------------------------------------------------------- speed & distance

_ROUND_TRIP = [
    (u, v) for u in range(10, 121, 5) for v in range(u + 5, 121, 5) if (2 * u * v) % (u + v) == 0
]
_CYCLIST_MAX_KMPH = 40


def _speed_distance(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    kmph = _unit("km/h", "km/h")
    if level == "easy":
        speed = rng.randrange(4, 25) * 5
        hours = rng.randrange(2, 9)
        distance = speed * hours
        text = f"A car covers {distance} km in {hours} hours. What is its average speed?"
        why = f"Speed = distance / time = {distance}/{hours} = {speed} km/h."
        slips = [Fraction(distance, hours + 1), Fraction(distance, hours - 1), speed + hours]
        return _q("speed_distance", text, _numeric_options(rng, speed, slips, 5, kmph), why), {
            "distance": distance,
            "hours": hours,
        }
    if level == "medium":
        u, v = rng.choice(_ROUND_TRIP)
        distance = rng.randrange(10, 91)
        ans = Fraction(2 * u * v, u + v)
        # the vehicle has to be believable at the faster speed
        traveller, goes = ("A cyclist", "rides") if v <= _CYCLIST_MAX_KMPH else ("A car", "travels")
        text = (
            f"{traveller} {goes} {distance} km to a town at {u} km/h and returns the same way "
            f"at {v} km/h. What is the average speed for the whole trip?"
        )
        why = (
            f"Average speed = total distance / total time = 2 x {u} x {v}/({u} + {v}) = "
            f"{fmt_num(ans)} km/h (not the simple mean of the two speeds)."
        )
        slips = [Fraction(u + v, 2), u + v, v - u]
        return _q("speed_distance", text, _numeric_options(rng, ans, slips, 1, kmph), why), {
            "distance": distance,
            "u": u,
            "v": v,
        }
    speed = rng.choice([36, 54, 72, 90, 108])
    per_sec = speed * 5 // 18
    while True:
        train = rng.randrange(5, 40) * 10
        platform = rng.randrange(5, 50) * 10
        if (train + platform) % per_sec == 0:
            break
    ans = (train + platform) // per_sec
    text = (
        f"A {train} m long train runs at {speed} km/h. How many seconds does it take to cross "
        f"a {platform} m long platform?"
    )
    why = (
        f"{speed} km/h = {speed} x 5/18 = {per_sec} m/s. Distance = train + platform = "
        f"{train + platform} m, so time = {train + platform}/{per_sec} = {plural(ans, 'second')}."
    )
    slips = [
        Fraction(train, per_sec),
        Fraction(train + platform, speed),
        Fraction(platform, per_sec),
    ]
    return _q("speed_distance", text, _numeric_options(rng, ans, slips, 1, _unit("second")), why), {
        "train_m": train,
        "platform_m": platform,
        "kmph": speed,
    }


# ---------------------------------------------------------------------------- simple interest


def _simple_interest(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    if level in ("easy", "medium"):
        principal = rng.randrange(5, 100) * 100
        rate = rng.randrange(2, 13)
        years = rng.randrange(2, 7)
        interest = principal * rate * years // 100
        one_year = principal * rate // 100
        facts = {"principal": principal, "rate": rate, "years": years}
        if level == "easy":
            text = (
                f"Find the simple interest on Rs {principal} at {rate}% per annum "
                f"for {years} years."
            )
            why = f"SI = P x R x T/100 = {principal} x {rate} x {years}/100 = {interest}."
            slips = [one_year, principal + interest, interest * 10]
            opts = _numeric_options(rng, interest, slips, one_year, _rupees)
            return _q("simple_interest", text, opts, why), facts
        amount = principal + interest
        compound = Fraction(principal) * Fraction(100 + rate, 100) ** years
        text = (
            f"Rs {principal} is invested at {rate}% simple interest per annum. "
            f"What is the amount after {years} years?"
        )
        why = f"SI = {principal} x {rate} x {years}/100 = {interest}; amount = {amount}."
        slips = [interest, principal + one_year, compound]
        opts = _numeric_options(rng, amount, slips, one_year, _rupees)
        return _q("simple_interest", text, opts, why), facts
    while True:
        principal = rng.randrange(2, 60) * 600
        rate = rng.choice([4, 5, 6, 8, 9, 10, 12])
        months = rng.choice([3, 6, 9, 15, 18, 20, 30])
        if (principal * rate * months) % 1200 == 0:
            break
    interest = principal * rate * months // 1200
    text = f"Find the simple interest on Rs {principal} at {rate}% per annum for {months} months."
    why = (
        f"{months} months = {months}/12 years. SI = {principal} x {rate} x {months}/(100 x 12) "
        f"= {interest}."
    )
    slips = [Fraction(principal * rate * months, 100), principal + interest]
    if months >= 12:
        slips.append(Fraction(principal * rate * (months // 12), 100))
    opts = _numeric_options(rng, interest, slips, Fraction(principal * rate, 1200), _rupees)
    return _q("simple_interest", text, opts, why), {
        "principal": principal,
        "rate": rate,
        "months": months,
    }


# ---------------------------------------------------------------------------- average


def _average(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    if level == "easy":
        n = rng.choice([4, 5, 6])
        numbers = [rng.randrange(10, 90) for _ in range(n)]
        numbers[-1] += -sum(numbers) % n
        total = sum(numbers)
        ans = total // n
        text = f"What is the average of {', '.join(str(x) for x in numbers)}?"
        why = f"Sum = {total}; there are {n} numbers, so the average = {total}/{n} = {ans}."
        median = sorted(numbers)[n // 2]
        slips = [Fraction(total, n - 1), Fraction(total, n + 1), median]
        return _q("average", text, _numeric_options(rng, ans, slips, 1), why), {"numbers": numbers}
    if level == "medium":
        while True:
            n = rng.randrange(5, 13)
            mean = rng.randrange(20, 81)
            new_mean = mean + rng.choice([-3, -2, -1, 1, 2, 3])
            removed = n * mean - (n - 1) * new_mean
            if removed > 0:
                break
        text = (
            f"The average of {n} numbers is {mean}. If the number {removed} is removed, "
            f"what is the average of the remaining numbers?"
        )
        why = (
            f"Sum = {n} x {mean} = {n * mean}; after removing {removed} the sum is "
            f"{n * mean - removed} over {n - 1} numbers: average {new_mean}."
        )
        slips = [Fraction(n * mean - removed, n), mean]
        return _q("average", text, _numeric_options(rng, new_mean, slips, 1), why), {
            "count": n,
            "mean": mean,
            "removed": removed,
        }
    while True:
        n1, n2 = rng.randrange(10, 51), rng.randrange(10, 51)
        a1, a2 = rng.randrange(40, 91), rng.randrange(40, 91)
        if n1 != n2 and a1 != a2 and (n1 * a1 + n2 * a2) % (n1 + n2) == 0:
            break
    ans = (n1 * a1 + n2 * a2) // (n1 + n2)
    text = (
        f"Section A has {n1} students with an average mark of {a1}; section B has {n2} "
        f"students with an average mark of {a2}. What is the average mark of all students?"
    )
    why = (
        f"Total marks = {n1} x {a1} + {n2} x {a2} = {n1 * a1 + n2 * a2} over {n1 + n2} "
        f"students: {ans}. The simple mean of {a1} and {a2} ignores the group sizes."
    )
    slips = [Fraction(a1 + a2, 2), Fraction(n1 * a2 + n2 * a1, n1 + n2)]
    return _q("average", text, _numeric_options(rng, ans, slips, 1), why), {
        "n1": n1,
        "a1": a1,
        "n2": n2,
        "a2": a2,
    }


# ---------------------------------------------------------------------------- mixture


def _mixture(rng: random.Random, level: Level) -> tuple[GeneratedQuestion, Facts]:
    litres_u = _unit("litre")
    if level == "easy":
        while True:
            litres = rng.randrange(2, 40) * 5
            pct = rng.choice([10, 20, 25, 30, 40, 50, 60, 75, 80])
            if (litres * pct) % 100 == 0:
                break
        ans = litres * pct // 100
        text = f"A {litres}-litre solution contains {pct}% acid. How many litres of acid are in it?"
        why = f"Acid = {pct}% of {litres} = {litres} x {pct}/100 = {plural(ans, 'litre')}."
        slips = [litres - ans, Fraction(litres * pct, 1000), ans * 10]
        return _q("mixture", text, _numeric_options(rng, ans, slips, 1, litres_u), why), {
            "litres": litres,
            "pct": pct,
        }
    if level == "medium":
        while True:
            litres = rng.randrange(2, 30) * 10
            target = rng.choice([5, 10, 20, 25, 40])
            pct = rng.choice([p for p in (10, 20, 25, 30, 40, 50, 60) if p > target])
            if (litres * (pct - target)) % target == 0:
                break
        ans = litres * (pct - target) // target
        text = (
            f"How many litres of water must be added to {litres} litres of a {pct}% salt "
            f"solution to make it a {target}% solution?"
        )
        why = (
            f"Salt = {pct}% of {litres} = {plural(Fraction(litres * pct, 100), 'litre')} "
            f"and stays fixed. It must be {target}% of the new volume, so the new volume "
            f"is {plural(litres + ans, 'litre')}: add {plural(ans, 'litre')} of water."
        )
        slips = [
            Fraction(litres * (pct - target), 100),
            Fraction(litres * (pct - target), pct),
            litres + ans,
        ]
        return _q("mixture", text, _numeric_options(rng, ans, slips, 1, litres_u), why), {
            "litres": litres,
            "pct": pct,
            "target_pct": target,
        }
    while True:
        litres = rng.randrange(2, 21) * 10
        removed = rng.randrange(2, litres // 2)
        if (litres - removed) ** 2 % litres == 0:
            break
    ans = (litres - removed) ** 2 // litres
    text = (
        f"A vessel holds {litres} litres of pure milk. {removed} litres are taken out and "
        f"replaced with water; this is done 2 times in total. How many litres of milk are left?"
    )
    why = (
        f"Each round keeps {litres - removed}/{litres} of the milk: {litres} x "
        f"({litres - removed}/{litres})^2 = {plural(ans, 'litre')}. "
        f"Subtracting {removed} twice ignores that the second draw is partly water."
    )
    slips = [litres - 2 * removed, litres - removed, litres - ans]
    return _q("mixture", text, _numeric_options(rng, ans, slips, 1, litres_u), why), {
        "litres": litres,
        "removed": removed,
        "times": 2,
    }


# ---------------------------------------------------------------------------- registry

_BUILDERS: dict[str, Builder] = {
    "percentage": _percentage,
    "profit_loss": _profit_loss,
    "ratio": _ratio,
    "time_work": _time_work,
    "speed_distance": _speed_distance,
    "simple_interest": _simple_interest,
    "average": _average,
    "mixture": _mixture,
}

APTITUDE: dict[str, Generator] = {name: public(b) for name, b in _BUILDERS.items()}


def _facts(topic: str, rng: random.Random, level: Level) -> Facts:
    """Test hook: the numbers behind the question the same rng state would generate."""
    return _BUILDERS[topic](rng, level)[1]

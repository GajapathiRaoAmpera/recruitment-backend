"""Dev seed: create 3 coding questions (3 sample + 15 hidden cases each) through the real
service, so audit_logs and validation are the same as POST /questions. Optionally link them
to a coding round's test (what PUT /rounds/{id}/test + PUT /tests/{id}/questions do).

    uv run python -m scripts.seed_coding_questions --hr-email hr@example.com
    uv run python -m scripts.seed_coding_questions --hr-email hr@example.com --round-id <uuid>
    uv run python -m scripts.seed_coding_questions --database-url postgresql+asyncpg://... ...

Uses DATABASE_URL unless --database-url is given, and refuses any host but localhost unless
--allow-remote is passed: this is for scratch databases. Questions that the same HR already
has (same body) are reused, not duplicated, so running it twice is harmless. Linking appends
to the test's current questions; it fails with TEST_IN_USE once a student has started.

Expected outputs come from the reference functions below, so they are right by construction.
"""

import argparse
import asyncio
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.schemas import StaffUser
from app.core.errors import AppError
from app.execution.coding_questions import service as questions
from app.execution.coding_questions.schemas import CaseIn, CodingQuestionIn
from app.execution.test_config import repo as test_repo
from app.execution.test_config import service as test_config
from app.execution.test_config.schemas import TestConfigIn

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TAG = "seed"


# --- reference solutions: they produce every expected output -----------------------------


def count_even(nums: list[int]) -> str:
    return f"{sum(1 for x in nums if x % 2 == 0)}\n"


def balanced(s: str) -> str:
    pairs, stack = {")": "(", "]": "[", "}": "{"}, []
    for ch in s:
        if ch in "([{":
            stack.append(ch)
        elif not stack or stack.pop() != pairs[ch]:
            return "NO\n"
    return "YES\n" if not stack else "NO\n"


def max_subarray(nums: list[int]) -> str:
    best = cur = nums[0]
    for x in nums[1:]:
        cur = max(x, cur + x)
        best = max(best, cur)
    return f"{best}\n"


def numbers_input(nums: list[int]) -> str:
    return f"{len(nums)}\n{' '.join(map(str, nums))}\n"


# --- the questions ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Seed:
    body: str
    difficulty: str
    marks: Decimal
    tags: list[str]
    cases: Callable[[], list[CaseIn]]


def _cases(inputs: list, to_stdin: Callable, solve: Callable) -> list[CaseIn]:
    assert len(inputs) == 18
    return [
        CaseIn(input=to_stdin(x), expected=solve(x), is_sample=i < 3) for i, x in enumerate(inputs)
    ]


def _even_cases() -> list[CaseIn]:
    rng = random.Random(101)
    fixed = [[1, 2, 3, 4], [7, 9, 11], [0], [-2, -3, -4], [2] * 5, [10**9, -(10**9), 1]]
    rand = [[rng.randint(-(10**6), 10**6) for _ in range(rng.choice([5, 50, 500, 1000]))]]
    rand += [[rng.randint(-(10**6), 10**6) for _ in range(rng.randint(1, 1000))] for _ in range(11)]
    return _cases(fixed + rand, numbers_input, count_even)


def _bracket_cases() -> list[CaseIn]:
    rng = random.Random(202)

    def valid(n: int) -> str:  # a random balanced string of n pairs
        out, stack, opened = [], [], 0
        while opened < n or stack:
            if opened < n and (not stack or rng.random() < 0.5):
                ch = rng.choice("([{")
                out.append(ch)
                stack.append(ch)
                opened += 1
            else:
                out.append({"(": ")", "[": "]", "{": "}"}[stack.pop()])
        return "".join(out)

    def broken(n: int) -> str:
        s = list(valid(n))
        i = rng.randrange(len(s))
        s[i] = {"(": "]", "[": "}", "{": ")", ")": "(", "]": "[", "}": "{"}[s[i]]
        return "".join(s)

    fixed = ["()[]{}", "([)]", "{[()()]}", "(", "]", "((((((((((", "{}{}{}", "([]{})[]", "{[}]"]
    rand = [valid(n) for n in (50, 500, 2000, 5000)] + [broken(n) for n in (50, 500, 2000)]
    rand += [valid(3000) + "(", ")" + valid(3000)]
    return _cases(fixed + rand, lambda s: f"{s}\n", balanced)


def _subarray_cases() -> list[CaseIn]:
    rng = random.Random(303)
    fixed = [
        [-2, 1, -3, 4, -1, 2, 1, -5, 4],
        [1, 2, 3],
        [-3, -1, -2],
        [5],
        [-7],
        [0, 0, 0],
        [10**4] * 1000,
        [-(10**4)] * 1000,
    ]
    rand = [[rng.randint(-(10**4), 10**4) for _ in range(rng.randint(1, 1000))] for _ in range(10)]
    return _cases(fixed + rand, numbers_input, max_subarray)


SEEDS = [
    Seed(
        body=(
            "Count the Even Numbers\n\n"
            "You are given n integers. Print how many of them are even.\n\n"
            "Input: line 1 has n (1 <= n <= 1000). Line 2 has n integers separated by spaces, "
            "each between -10^9 and 10^9.\n"
            "Output: one integer, the count of even numbers.\n\n"
            "Example: for 4 numbers 1 2 3 4 the answer is 2."
        ),
        difficulty="easy",
        marks=Decimal(5),
        tags=[TAG, "arrays"],
        cases=_even_cases,
    ),
    Seed(
        body=(
            "Balanced Brackets\n\n"
            "A string contains only the characters ( ) [ ] { }. It is balanced when every "
            "opening bracket is closed by the matching bracket in the right order. Print YES if "
            "the string is balanced, otherwise NO.\n\n"
            "Input: one line with the string (1 <= length <= 10000).\n"
            "Output: YES or NO.\n\n"
            "Example: ()[]{} is balanced, ([)] is not."
        ),
        difficulty="medium",
        marks=Decimal(10),
        tags=[TAG, "stack", "strings"],
        cases=_bracket_cases,
    ),
    Seed(
        body=(
            "Maximum Subarray Sum\n\n"
            "You are given n integers. Find the largest sum of a non-empty run of consecutive "
            "numbers and print it.\n\n"
            "Input: line 1 has n (1 <= n <= 1000). Line 2 has n integers separated by spaces, "
            "each between -10^4 and 10^4.\n"
            "Output: one integer, the largest sum.\n\n"
            "Example: for -2 1 -3 4 -1 2 1 -5 4 the answer is 6 (from 4 -1 2 1)."
        ),
        difficulty="medium",
        marks=Decimal(10),
        tags=[TAG, "arrays", "dynamic-programming"],
        cases=_subarray_cases,
    ),
]


def question_payloads() -> list[CodingQuestionIn]:
    """Validated exactly like a POST /questions body (3 sample + 15 hidden, sizes, tags)."""
    return [
        CodingQuestionIn(
            kind="coding",
            body=s.body,
            difficulty=s.difficulty,
            tags=s.tags,
            marks=s.marks,
            test_cases=s.cases(),
        )
        for s in SEEDS
    ]


# --- database --------------------------------------------------------------------------------


def check_target(url: str, allow_remote: bool) -> str:
    """The host to print. Refuses anything but localhost unless --allow-remote."""
    host = make_url(url).host or "localhost"
    if host not in LOCAL_HOSTS and not allow_remote:
        raise SystemExit(
            f"refusing to seed {host}: this script is for scratch databases. "
            "Pass --allow-remote only if that database is meant for test data."
        )
    return host


async def staff_user(session: AsyncSession, email: str) -> StaffUser:
    row = (
        (
            await session.execute(
                text(
                    "select id, email, full_name, role from users "
                    "where email = :e and role in ('hr', 'super_admin')"
                ),
                {"e": email},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise SystemExit(f"no HR or Super Admin user with email {email}")
    return StaffUser.model_validate(dict(row))


async def existing_question(session: AsyncSession, owner: UUID, body: str) -> UUID | None:
    return (
        await session.execute(
            text(
                "select id from questions where kind = 'coding' and created_by = :o "
                "and body = :b order by created_at limit 1"
            ),
            {"o": owner, "b": body},
        )
    ).scalar_one_or_none()


async def seed(session: AsyncSession, user: StaffUser, round_id: UUID | None) -> list[UUID]:
    ids: list[UUID] = []
    for payload in question_payloads():
        found = await existing_question(session, user.id, payload.body)
        if found:
            print(f"  exists   {found}  {payload.body.splitlines()[0]}")
            ids.append(found)
            continue
        created = await questions.create(session, user, payload)
        print(f"  created  {created.id}  {payload.body.splitlines()[0]} ({payload.difficulty})")
        ids.append(created.id)
    if round_id is not None:
        test = await test_config.configure(session, user, round_id, TestConfigIn())
        current = [r["question_id"] for r in await test_repo.current_questions(session, test.id)]
        wanted = current + [q for q in ids if q not in current]
        out = await test_config.set_questions(session, user, test.id, wanted)
        print(f"  test {test.id}: {out.question_count} questions, levels {out.levels}")
    return ids


async def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--hr-email", required=True, help="owner of the questions (HR or Super Admin)")
    p.add_argument("--round-id", type=UUID, help="coding round whose test gets the questions")
    p.add_argument("--database-url", help="defaults to DATABASE_URL")
    p.add_argument("--allow-remote", action="store_true", help="allow a non-localhost database")
    args = p.parse_args(argv)
    if args.database_url:
        url = args.database_url
    else:
        from app.core.config import get_settings

        url = get_settings().database_url
    host = check_target(url, args.allow_remote)
    engine = create_async_engine(url, connect_args={"statement_cache_size": 0})
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            user = await staff_user(session, args.hr_email)
            print(f"seeding on {host} as {user.email}")
            await seed(session, user, args.round_id)
    except AppError as e:
        raise SystemExit(f"{e.code}: {e.message}") from e
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))

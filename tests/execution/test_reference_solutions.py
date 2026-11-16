"""AC-5: a reference solution per language passes all 18 cases (3 sample + 15 hidden).

INTEGRATION test: it talks to a real, running Judge0 (`make judge0-up`) and is skipped by
default. Run it with `make test-judge0`. Unit tests in this folder mock Judge0; this one must not.

The problem is "sum of numbers": line 1 is n, line 2 has n integers, print their sum. Cases
include negatives, n = 1, zeros and sums above 2**31, so a solution using 32-bit ints fails.

SQL is the odd one out. The SQLite language ignores stdin and the sandbox is read-only
(`create table`, even `create temp table`, fails with "access permission denied"), so a SQL
question can only be a read-only query over data written into the query itself (e.g. a CTE).
It cannot take a different input per case; its 18 cases share one dataset. Decide how SQL
questions get per-case data before building the question editor.
"""

import asyncio
import random

import httpx
import pytest

from app.execution.judge0 import Judge0Client
from app.execution.judging import Verdict
from app.execution.languages import LANGUAGES
from app.execution.scoring import CaseSpec, run_samples, submit

pytestmark = pytest.mark.integration

SOLUTIONS = {
    "python": "n = int(input())\nprint(sum(map(int, input().split())))\n",
    "javascript": (
        "const t = require('fs').readFileSync(0, 'utf8').split('\\n');\n"
        "console.log(t[1].split(' ').filter(Boolean).reduce((a, b) => a + Number(b), 0));\n"
    ),
    "c": (
        '#include <stdio.h>\nint main(void){int n;scanf("%d",&n);long long s=0,x;\n'
        'for(int i=0;i<n;i++){scanf("%lld",&x);s+=x;}printf("%lld\\n",s);return 0;}\n'
    ),
    "cpp": (
        "#include <iostream>\nint main(){int n;std::cin>>n;long long s=0,x;\n"
        "for(int i=0;i<n;i++){std::cin>>x;s+=x;}std::cout<<s<<std::endl;}\n"
    ),
    "java": (
        "import java.util.Scanner;\npublic class Main {\n"
        "  public static void main(String[] a) {\n"
        "    Scanner in = new Scanner(System.in);\n"
        "    int n = in.nextInt();\n    long s = 0;\n"
        "    for (int i = 0; i < n; i++) s += in.nextLong();\n"
        "    System.out.println(s);\n  }\n}\n"
    ),
    "go": (
        'package main\n\nimport "fmt"\n\nfunc main() {\n'
        "\tvar n int\n\tfmt.Scan(&n)\n\tvar s, x int64\n"
        "\tfor i := 0; i < n; i++ {\n\t\tfmt.Scan(&x)\n\t\ts += x\n\t}\n\tfmt.Println(s)\n}\n"
    ),
}

SQL_DATA = [3_000_000_000, -7, 12, 0, 2_500_000_000]
SQL_SOLUTION = (
    "with t(a) as (values " + ", ".join(f"({v})" for v in SQL_DATA) + ") select sum(a) from t;\n"
)


def sum_cases() -> list[CaseSpec]:
    rng = random.Random(4_3)  # fixed seed: the same 18 cases every run
    numbers = [
        [1, 2, 3],  # samples
        [-5, 5, -10, 4],
        [7],
        [0],  # hidden from here
        [-1, -2, -3],
        [0, 0, 0, 0],
        [2_000_000_000, 2_000_000_000, 2_000_000_000],  # overflows 32-bit
        [-2_000_000_000, -2_000_000_000, -2_000_000_000],
        [1_000_000_000] * 50,
        [-1_000_000_000] * 3 + [1],
    ] + [
        [rng.randint(-(10**9), 10**9) for _ in range(rng.choice([2, 10, 100, 1000]))]
        for _ in range(8)
    ]
    return [
        CaseSpec(f"{len(nums)}\n{' '.join(map(str, nums))}\n", f"{sum(nums)}\n", i < 3)
        for i, nums in enumerate(numbers)
    ]


def sql_cases() -> list[CaseSpec]:
    return [CaseSpec("", f"{sum(SQL_DATA)}\n", i < 3) for i in range(18)]


@pytest.fixture(scope="module")
def judge0() -> Judge0Client:
    client = Judge0Client()
    try:
        r = httpx.get(f"{client._base}/languages", headers=client._headers, timeout=3)
        ok = r.status_code == 200
    except httpx.HTTPError:
        ok = False
    if not ok:
        pytest.skip("Judge0 is not reachable: start it with `make judge0-up`")
    return client


def reference(language: str) -> tuple[str, list[CaseSpec]]:
    if language == "sql":
        return SQL_SOLUTION, sql_cases()
    return SOLUTIONS[language], sum_cases()


def test_every_language_has_a_reference_solution():
    assert set(SOLUTIONS) | {"sql"} == set(LANGUAGES)


def test_case_set_has_three_samples_and_fifteen_hidden():
    for cases in (sum_cases(), sql_cases()):
        assert len(cases) == 18 and sum(c.is_sample for c in cases) == 3


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_reference_solution_passes_all_18(judge0, language):
    code, cases = reference(language)
    result = asyncio.run(submit(judge0, language, code, cases))
    assert (result.passed, result.total) == (18, 18), result.verdicts
    assert result.verdicts == {Verdict.PASSED: 18}


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_run_shows_the_three_samples_passing(judge0, language):
    code, cases = reference(language)
    judged = asyncio.run(run_samples(judge0, language, code, cases))
    assert [j.verdict for j in judged] == [Verdict.PASSED] * 3


def test_a_wrong_solution_is_not_scored_as_passing(judge0):
    """Guards against a vacuous harness: a program that is wrong must lose cases."""
    code = "input()\nprint(sum(map(int, input().split())) + 1)\n"
    result = asyncio.run(submit(judge0, "python", code, sum_cases()))
    assert result.passed == 0 and result.verdicts == {Verdict.WRONG_ANSWER: 18}


def test_a_32_bit_solution_fails_the_overflow_cases(judge0):
    code = SOLUTIONS["cpp"].replace("long long", "int")
    result = asyncio.run(submit(judge0, "cpp", code, sum_cases()))
    assert 0 < result.passed < 18

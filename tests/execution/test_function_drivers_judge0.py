"""Function-mode drivers on a REAL Judge0: every language x every type round-trips.

INTEGRATION test (skipped by default; run with `make test-judge0` after `make judge0-up`).

Each solution is the generated starter code with an "echo" body filled in, so this also proves
the starter stubs compile. Every echo first prints "debug" to stdout: the driver sends the
student's stdout to stderr, so the case must still pass. Expected outputs come from values.py
(the Python side of the canonical format), so a driver that formats differently fails here.
"""

import asyncio
import json

import httpx
import pytest

from app.execution.function_mode import values
from app.execution.function_mode.drivers import build_source
from app.execution.function_mode.signature import Signature
from app.execution.function_mode.starter import FUNCTION_LANGUAGES, starter_code
from app.execution.judge0 import Judge0Client
from app.execution.judging import Verdict
from app.execution.scoring import CaseSpec, run_samples, submit

pytestmark = pytest.mark.integration

# JSON values per type, including the awkward ones (ties when rounding, -0, escapes, unicode).
VALUES = {
    "int": [0, -2147483648, 2147483647, 42],
    "long": [-9007199254740991, 9007199254740991, 0, 3000000000],
    "double": [0.015625, -0.015625, 2.5, -1e-07, 123456.789, 0.123455, 1e16, 3],
    "bool": [True, False],
    "string": ["", 'a"b\\c', "tab\tnl\nx\r", "héllo ✓ 😀", "\u0001\u001f\u007f", "/s "],
    "int[]": [[], [1, -2, 3], [2147483647, -2147483648]],
    "string[]": [[], ["a", "", "x y"], ["😀", '"q"', "\\"]],
    "int[][]": [[], [[]], [[1, 2], [3], []]],
}


def echo_signature(type_: str) -> Signature:
    return Signature.model_validate(
        {
            "function_name": "echoValue",
            "parameters": [{"name": "value", "type": type_}],
            "return_type": type_,
        }
    )


def echo_solution(language: str, type_: str) -> str:
    stub = starter_code(echo_signature(type_))[language]
    if language == "python":
        return stub.replace("    pass\n", '    print("debug")\n    return value\n')
    if language == "javascript":
        return stub.replace("{\n    \n}", "{\n    console.log('debug');\n    return value;\n}")
    if language == "c":
        body = 'printf("debug\\n");'
        if type_ in ("int[]", "string[]", "int[][]"):
            body += " *returnSize = valueSize;"
        if type_ == "int[][]":
            body += " *returnColumnSizes = valueColSize;"
        return stub.replace("{\n    \n}", "{\n    " + body + "\n    return value;\n}")
    if language == "cpp":
        body = 'cout << "debug" << endl; printf("debug\\n"); return value;'
        return stub.replace("{\n        \n    }", "{\n        " + body + "\n    }")
    if language == "java":
        body = 'System.out.println("debug"); return value;'
        return stub.replace("{\n        \n    }", "{\n        " + body + "\n    }")
    # go: the student's own import, and no package clause (the driver adds it)
    return 'import "fmt"\n\n' + stub.replace(
        "{\n    \n}", '{\n    fmt.Println("debug")\n    return value\n}'
    )


def cases_for(type_: str) -> list[CaseSpec]:
    out = []
    for v in VALUES[type_]:
        checked = values.check_value(type_, v)
        stdin = values.format_value(type_, checked) + "\n"
        expected = values.format_value(type_, checked, is_input=False) + "\n"
        out.append(CaseSpec(stdin, expected, True))
    return out


@pytest.fixture(scope="module")
def judge0() -> Judge0Client:
    client = Judge0Client()
    try:
        ok = httpx.get(f"{client._base}/languages", headers=client._headers, timeout=3).is_success
    except httpx.HTTPError:
        ok = False
    if not ok:
        pytest.skip("Judge0 is not reachable: start it with `make judge0-up`")
    return client


def test_expected_outputs_are_the_documented_canonical_forms():
    got = [c.expected.strip() for c in cases_for("double")]
    assert got[:4] == ["0.01562", "-0.01562", "2.50000", "0.00000"]  # half-even; no "-0.00000"
    assert [c.expected.strip() for c in cases_for("string")][1] == json.dumps('a"b\\c')


@pytest.mark.parametrize("type_", list(VALUES))
@pytest.mark.parametrize("language", FUNCTION_LANGUAGES)
def test_round_trip(judge0, language, type_):
    sig = echo_signature(type_)
    source = build_source(language, sig, echo_solution(language, type_))
    cases = cases_for(type_)
    judged = asyncio.run(run_samples(judge0, language, source, cases))
    detail = [
        (c.input, c.expected, j.verdict, j.stdout, j.detail)
        for c, j in zip(cases, judged, strict=True)
    ]
    assert all(j.verdict is Verdict.PASSED for j in judged), detail


SUM_SIG = Signature.model_validate(
    {
        "function_name": "weightedSum",
        "parameters": [
            {"name": "a", "type": "int"},
            {"name": "b", "type": "long"},
            {"name": "nums", "type": "int[]"},
            {"name": "grid", "type": "int[][]"},
            {"name": "words", "type": "string[]"},
            {"name": "flag", "type": "bool"},
        ],
        "return_type": "long",
    }
)
# a + 2b + 3 sum(nums) + 5 sum(grid) + 7 total word length, negated when flag is false
SUM_SOLUTIONS = {
    "python": (
        "def weightedSum(a, b, nums, grid, words, flag):\n"
        "    s = a + 2 * b + 3 * sum(nums) + 5 * sum(map(sum, grid)) + 7 * sum(map(len, words))\n"
        "    return s if flag else -s\n"
    ),
    "javascript": (
        "function weightedSum(a, b, nums, grid, words, flag) {\n"
        "  let s = a + 2 * b + 3 * nums.reduce((x, y) => x + y, 0);\n"
        "  for (const r of grid) for (const v of r) s += 5 * v;\n"
        "  for (const w of words) s += 7 * w.length;\n"
        "  return flag ? s : -s;\n}\n"
    ),
    "c": (
        "long long weightedSum(int a, long long b, int* nums, int numsSize, int** grid,\n"
        "    int gridSize, int* gridColSize, char** words, int wordsSize, bool flag) {\n"
        "    long long s = a + 2 * b;\n"
        "    for (int i = 0; i < numsSize; i++) s += 3LL * nums[i];\n"
        "    for (int i = 0; i < gridSize; i++)\n"
        "        for (int j = 0; j < gridColSize[i]; j++) s += 5LL * grid[i][j];\n"
        "    for (int i = 0; i < wordsSize; i++) s += 7LL * (long long)strlen(words[i]);\n"
        "    return flag ? s : -s;\n}\n"
    ),
    "cpp": (
        "class Solution {\npublic:\n"
        "    long long weightedSum(int a, long long b, vector<int>& nums,\n"
        "        vector<vector<int>>& grid, vector<string>& words, bool flag) {\n"
        "        long long s = a + 2 * b;\n"
        "        for (int v : nums) s += 3LL * v;\n"
        "        for (auto& r : grid) for (int v : r) s += 5LL * v;\n"
        "        for (auto& w : words) s += 7LL * (long long)w.size();\n"
        "        return flag ? s : -s;\n    }\n};\n"
    ),
    "java": (
        "class Solution {\n"
        "    public long weightedSum(int a, long b, int[] nums, int[][] grid, String[] words,\n"
        "            boolean flag) {\n"
        "        long s = a + 2 * b;\n"
        "        for (int v : nums) s += 3L * v;\n"
        "        for (int[] r : grid) for (int v : r) s += 5L * v;\n"
        "        for (String w : words) s += 7L * w.length();\n"
        "        return flag ? s : -s;\n    }\n}\n"
    ),
    "go": (
        "package main\n\n"
        "func weightedSum(a int, b int64, nums []int, grid [][]int, words []string, "
        "flag bool) int64 {\n"
        "\ts := int64(a) + 2*b\n"
        "\tfor _, v := range nums {\n\t\ts += 3 * int64(v)\n\t}\n"
        "\tfor _, r := range grid {\n"
        "\t\tfor _, v := range r {\n\t\t\ts += 5 * int64(v)\n\t\t}\n\t}\n"
        "\tfor _, w := range words {\n\t\ts += 7 * int64(len(w))\n\t}\n"
        "\tif !flag {\n\t\treturn -s\n\t}\n\treturn s\n}\n"
    ),
}


def sum_cases() -> list[CaseSpec]:
    rows = [  # ascii words: every language counts the same length
        (1, 2, [3], [[4]], ["ab"], True),
        (-5, 10**12, [], [], [], False),
        (7, -3, [1, 2, 3], [[1], [], [2, 3]], ["", "xyz"], True),
    ] + [(i, i * 1000, [i] * i, [[i, -i, i]], ["w" * i], i % 2 == 0) for i in range(15)]
    out = []
    for i, (a, b, nums, grid, words, flag) in enumerate(rows):
        args = {"a": a, "b": b, "nums": nums, "grid": grid, "words": words, "flag": flag}
        s = a + 2 * b + 3 * sum(nums) + 5 * sum(map(sum, grid)) + 7 * sum(map(len, words))
        out.append(CaseSpec(values.canonical_args(SUM_SIG, args), f"{s if flag else -s}\n", i < 3))
    return out


def test_every_language_has_a_sum_solution():
    assert set(SUM_SOLUTIONS) == set(FUNCTION_LANGUAGES)


@pytest.mark.parametrize("language", FUNCTION_LANGUAGES)
def test_six_parameters_submit_all_18(judge0, language):
    """Several parameters, in order, through Submit's path (3 sample + 15 hidden, one batch)."""
    source = build_source(language, SUM_SIG, SUM_SOLUTIONS[language])
    result = asyncio.run(submit(judge0, language, source, sum_cases()))
    assert (result.passed, result.total) == (18, 18), result.verdicts


# A compile (or syntax) error on the student's line 3 is reported as line 3.
BROKEN = {
    "python": ("def echoValue(value):\n    x = 1\n    return value +* 2\n", "line 3"),
    "javascript": ("function echoValue(value) {\n  let x = 1;\n  return value +* ;\n}\n", ":3"),
    "c": ("int echoValue(int value) {\n    int x = 1;\n    return value + nope;\n}\n", ":3:"),
    "cpp": (
        "class Solution {\npublic:\n    int echoValue(int value) { return value + nope; }\n};\n",
        ":3:",
    ),
    "java": (
        "class Solution {\n    public int echoValue(int value) {\n        return value + nope;\n"
        "    }\n}\n",
        ":3:",
    ),
    "go": ("package main\n\nfunc echoValue(value int) int { return value + nope }\n", ":3:"),
}


@pytest.mark.parametrize("language", FUNCTION_LANGUAGES)
def test_error_line_numbers_match_the_students_code(judge0, language):
    code, marker = BROKEN[language]
    source = build_source(language, echo_signature("int"), code)
    [j] = asyncio.run(run_samples(judge0, language, source, [CaseSpec("1\n", "1\n", True)]))
    assert j.verdict in (Verdict.COMPILE_ERROR, Verdict.RUNTIME_ERROR), j
    assert marker in j.detail, j.detail
    assert "driver" not in j.detail.split(marker)[0].splitlines()[-1], j.detail


@pytest.mark.parametrize(
    "language,code",
    [
        ("python", "def echoValue(value):\n    return None\n"),
        ("javascript", "function echoValue(value) {\n  return 'x';\n}\n"),
        (
            "java",
            "class Solution {\n    public String echoValue(String value) { return null; }\n}\n",
        ),
    ],
)
def test_a_wrong_return_value_is_a_runtime_error_that_says_why(judge0, language, code):
    type_ = "string" if language == "java" else "int"
    source = build_source(language, echo_signature(type_), code)
    stdin = "1\n" if type_ == "int" else '"a"\n'
    [j] = asyncio.run(run_samples(judge0, language, source, [CaseSpec(stdin, stdin, True)]))
    assert j.verdict is Verdict.RUNTIME_ERROR and "echoValue returned" in j.detail, j

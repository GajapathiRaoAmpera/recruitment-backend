"""Function-mode questions without Judge0: signature rules, canonical values, starter code,
build_source (student code first, line numbers kept), HR create/edit and the student view.

Real Judge0 round-trips per language and type: test_function_drivers_judge0.py (integration).
"""

import subprocess
import sys
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.attempts import state_repo as attempt_repo
from app.auth import entry as auth_entry
from app.core.db import get_session
from app.core.errors import AppError
from app.execution.function_mode import drivers, values
from app.execution.function_mode.drivers import build_source
from app.execution.function_mode.signature import Signature, load_signature
from app.execution.function_mode.starter import FUNCTION_LANGUAGES, starter_code
from app.main import app
from tests.execution import test_coding_questions as cq

db = cq.db  # the faked coding-question repo (fixture)

TWO_SUM = {
    "function_name": "twoSum",
    "parameters": [{"name": "nums", "type": "int[]"}, {"name": "target", "type": "int"}],
    "return_type": "int[]",
}
SIG = Signature.model_validate(TWO_SUM)


def sig(**over) -> dict:
    return {**TWO_SUM, **over}


# --- signature -------------------------------------------------------------------------------


def test_every_allowed_type_is_accepted():
    types = ["int", "long", "double", "bool", "string", "int[]"]
    s = Signature.model_validate(
        sig(
            parameters=[{"name": f"p{i}", "type": t} for i, t in enumerate(types)],
            return_type="int[][]",
        )
    )
    assert s.types == types and s.return_type == "int[][]"


@pytest.mark.parametrize(
    "bad",
    [
        sig(return_type="float"),
        sig(return_type="int[][][]"),
        sig(parameters=[{"name": "nums", "type": "List[int]"}]),
        sig(function_name="solve"),  # one word: could clash with a C library name
        sig(function_name="TwoSum"),
        sig(function_name="two_sum"),
        sig(function_name="drvMain"),
        sig(function_name="1twoSum"),
        sig(parameters=[{"name": "class", "type": "int"}]),
        sig(parameters=[{"name": "string", "type": "int"}]),  # a type in C++ / Go
        sig(parameters=[{"name": "len", "type": "int"}]),
        sig(parameters=[{"name": "drvA0", "type": "int"}]),
        sig(parameters=[{"name": "my_list", "type": "int"}]),
        sig(parameters=[{"name": "a", "type": "int"}, {"name": "a", "type": "long"}]),
        sig(parameters=[{"name": "nums", "type": "int[]"}, {"name": "numsSize", "type": "int"}]),
        sig(parameters=[{"name": "twoSum", "type": "int"}]),
        sig(parameters=[]),
        sig(parameters=[{"name": f"p{i}", "type": "int"} for i in range(7)]),
        sig(parameters=[{"name": "a", "type": "int", "default": 1}]),
        {**TWO_SUM, "extra": 1},
    ],
)
def test_bad_signatures_are_rejected(bad):
    with pytest.raises(ValidationError):
        Signature.model_validate(bad)


def test_signature_loads_from_jsonb_text_or_dict():
    assert load_signature(SIG.model_dump_json()) == SIG == load_signature(TWO_SUM)
    assert load_signature(None) is None


# --- canonical values ------------------------------------------------------------------------


def test_hr_input_is_stored_canonical_and_shown_readably():
    stored = values.canonical_input(SIG, "[1, 2,  3]\r\n 5 \n\n")
    assert stored == "[1,2,3]\n5\n"
    assert values.display_input(SIG, stored) == "nums = [1,2,3], target = 5"
    assert values.canonical_expected(SIG, " [0, 1] \n") == "[0,1]\n"


@pytest.mark.parametrize(
    "type_,value,text",
    [
        ("int", -5, "-5"),
        ("long", 9007199254740991, "9007199254740991"),
        ("double", 2, "2.0"),
        ("double", 1e-07, "1e-07"),
        ("bool", True, "true"),
        ("string", 'a"b\né', '"a\\"b\\né"'),
        ("string[]", ["x", ""], '["x",""]'),
        ("int[][]", [[1], []], "[[1],[]]"),
    ],
)
def test_canonical_input_forms(type_, value, text):
    assert values.format_value(type_, values.check_value(type_, value)) == text


@pytest.mark.parametrize(
    "value,text", [(1 / 3, "0.33333"), (0.015625, "0.01562"), (-1e-9, "0.00000"), (2, "2.00000")]
)
def test_double_outputs_have_five_decimals_half_even_and_no_negative_zero(value, text):
    assert values.format_value("double", value, is_input=False) == text


@pytest.mark.parametrize(
    "type_,value",
    [
        ("int", True),  # bool is not an int
        ("int", 2**31),
        ("int", 1.0),
        ("long", 2**53),  # inputs stay exact in JavaScript
        ("bool", 1),
        ("string", 5),
        ("int[]", [1, "2"]),
        ("int[][]", [1, 2]),
        ("double", 10**400),
        ("string", "\ud800"),
    ],
)
def test_values_of_the_wrong_type_are_rejected(type_, value):
    with pytest.raises(ValueError):
        values.check_value(type_, value)


@pytest.mark.parametrize("text", ["[1,2]\n", "[1,2]\n3\n4\n", "[1,2\n3\n", "[1,2]\nNaN\n"])
def test_bad_case_inputs_are_rejected(text):
    with pytest.raises(ValueError):
        values.canonical_input(SIG, text)


def test_long_outputs_may_use_all_64_bits():
    long_sig = Signature.model_validate(sig(return_type="long"))
    assert values.canonical_expected(long_sig, str(2**63 - 1)) == f"{2**63 - 1}\n"


# --- starter code ----------------------------------------------------------------------------


def test_starter_code_is_a_stub_for_every_language_but_sql():
    stubs = starter_code(SIG)
    assert set(stubs) == set(FUNCTION_LANGUAGES) and "sql" not in stubs
    assert stubs["python"] == (
        "def twoSum(nums, target):\n    # nums: List[int], target: int -> List[int]\n    pass\n"
    )
    assert "function twoSum(nums, target) {" in stubs["javascript"]
    assert "int* twoSum(int* nums, int numsSize, int target, int* returnSize) {" in stubs["c"]
    assert "vector<int> twoSum(vector<int>& nums, int target) {" in stubs["cpp"]
    assert "public int[] twoSum(int[] nums, int target) {" in stubs["java"]
    assert stubs["go"].startswith("func twoSum(nums []int, target int) []int {")
    reading = ("stdin", "input(", "readFileSync", "Scanner", "scanf", "cin", "Stdin", "main")
    for code in stubs.values():
        assert not any(r in code for r in reading), code
    assert starter_code(SIG) == stubs  # deterministic


# --- build_source ----------------------------------------------------------------------------

CODE = "line one\nline two\nline three\n"


@pytest.mark.parametrize("language", ["python", "javascript"])
def test_student_code_comes_first(language):
    assert build_source(language, SIG, CODE).startswith(CODE)


@pytest.mark.parametrize(
    "language,prelude", [("c", drivers.C_PRELUDE), ("cpp", drivers.CPP_PRELUDE)]
)
def test_c_and_cpp_restart_line_numbers_at_the_student_code(language, prelude):
    src = build_source(language, SIG, CODE)
    assert prelude.endswith("#line 1\n") and src.startswith(prelude + CODE)


def test_java_keeps_line_one_and_drops_public_from_solution():
    src = build_source("java", SIG, "public class Solution {\n}\n")
    assert src.startswith(drivers.JAVA_HEADER + " class Solution {\n}\n")


@pytest.mark.parametrize(
    "code",
    [
        'package main\n\nimport "sort"\n\nfunc twoSum() {}\n',
        "// my solution\npackage main\nfunc twoSum() {}\n",
        "func twoSum() {}\n",
    ],
)
def test_go_imports_go_on_the_package_line_so_nothing_shifts(code):
    src = build_source("go", SIG, code)
    student_lines = code.splitlines()
    got = src.splitlines()[: len(student_lines)]
    for i, (g, line) in enumerate(zip(got, student_lines, strict=True)):
        if line.startswith("package") or (i == 0 and "package" not in code):
            assert drivers.GO_HEADER in g and g.endswith(line.replace("package main", ""))
        else:
            assert g == line  # every other line is the student's, at the same number
    assert src.count("package main") == 1 and drivers.GO_HEADER in src


def test_sql_is_not_allowed_in_function_mode():
    with pytest.raises(AppError) as e:
        build_source("sql", SIG, "select 1;")
    assert e.value.status == 422 and e.value.code == "LANGUAGE_NOT_ALLOWED"


def test_the_python_driver_runs_and_prints_the_canonical_result():
    """Local smoke run of the Python driver (Judge0 runs every language in the integration
    test). The student's print goes to stderr, the result is the only stdout line."""
    code = "def twoSum(nums, target):\n    print('debug')\n    return [len(nums), target]\n"
    p = subprocess.run(
        [sys.executable, "-I", "-c", build_source("python", SIG, code)],
        input=b"[4,5,6]\n-2\n",
        capture_output=True,
        timeout=20,
    )
    assert p.returncode == 0, p.stderr
    assert p.stdout == b"[3,-2]\n" and b"debug" in p.stderr


def test_the_python_driver_explains_a_wrong_return_type():
    code = "def twoSum(nums, target):\n    return None\n"
    p = subprocess.run(
        [sys.executable, "-I", "-c", build_source("python", SIG, code)],
        input=b"[1]\n2\n",
        capture_output=True,
        timeout=20,
    )
    assert p.returncode == 1 and b"twoSum returned None, but it must return int[]" in p.stderr


# --- HR create / edit ------------------------------------------------------------------------


def fn_cases():
    return [
        {"input": f"[{i}, 1]\n{i + 1}", "expected": "[0, 1]", "is_sample": i < 3} for i in range(18)
    ]


def fn_body(**over):
    return cq.body(**{"mode": "function", "signature": TWO_SUM, "test_cases": fn_cases(), **over})


def test_create_function_question_stores_canonical_cases_and_returns_mode(db):
    r = cq.as_user(cq.HR).post("/api/v1/questions", json=fn_body())
    assert r.status_code == 201, r.text
    q = r.json()
    assert q["mode"] == "function" and q["signature"] == TWO_SUM
    assert q["test_cases"][0]["input"] == "[0,1]\n1\n"
    assert q["test_cases"][0]["expected"] == "[0,1]\n"
    stored = db["questions"][uuid.UUID(q["id"])]
    assert stored["mode"] == "function" and stored["signature"] == TWO_SUM


def test_stdin_questions_default_to_stdin_mode_and_keep_their_cases(db):
    r = cq.as_user(cq.HR).post("/api/v1/questions", json=cq.body())
    assert r.status_code == 201 and r.json()["mode"] == "stdin" and r.json()["signature"] is None
    assert r.json()["test_cases"][0]["input"] == "s0"  # untouched


@pytest.mark.parametrize(
    "payload",
    [
        cq.body(mode="function"),  # no signature
        cq.body(signature=TWO_SUM),  # stdin with a signature
        fn_body(signature=sig(return_type="float")),
        fn_body(signature=sig(function_name="solve")),
        cq.body(mode="function", signature=TWO_SUM),  # "s0" is not an int[] + int
        cq.body(mode="lambda"),
    ],
)
def test_create_rejects_bad_mode_signature_or_cases(db, payload):
    r = cq.as_user(cq.HR).post("/api/v1/questions", json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert db["questions"] == {}


def created(db, payload) -> str:
    r = cq.as_user(cq.HR).post("/api/v1/questions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_patch_signature_revalidates_the_stored_cases(db):
    qid = created(db, fn_body())
    c = cq.as_user(cq.HR)
    renamed = sig(function_name="pairIndices")
    r = c.patch(f"/api/v1/questions/{qid}", json={"signature": renamed})
    assert r.status_code == 200 and r.json()["signature"]["function_name"] == "pairIndices"
    bad = sig(parameters=[{"name": "nums", "type": "string"}, {"name": "target", "type": "int"}])
    r = c.patch(f"/api/v1/questions/{qid}", json={"signature": bad})
    assert r.status_code == 422 and "test case 1" in r.text


def test_patch_to_stdin_clears_the_signature_and_to_function_needs_one(db):
    qid = created(db, fn_body())
    c = cq.as_user(cq.HR)
    r = c.patch(f"/api/v1/questions/{qid}", json={"mode": "stdin"})
    assert r.status_code == 200 and r.json()["mode"] == "stdin" and r.json()["signature"] is None
    r = c.patch(f"/api/v1/questions/{qid}", json={"mode": "function"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = c.patch(f"/api/v1/questions/{qid}", json={"mode": "stdin", "signature": TWO_SUM})
    assert r.status_code == 422


def test_patch_new_cases_on_a_function_question_are_checked_and_canonical(db):
    qid = created(db, fn_body())
    c = cq.as_user(cq.HR)
    r = c.patch(f"/api/v1/questions/{qid}", json={"test_cases": cq.cases()})
    assert r.status_code == 422
    cases = fn_cases()
    cases[0]["expected"] = "[ 9 ,9 ]"
    r = c.patch(f"/api/v1/questions/{qid}", json={"test_cases": cases})
    assert r.status_code == 200 and r.json()["test_cases"][0]["expected"] == "[9,9]\n"


def test_mode_and_signature_cannot_change_once_students_answered(db):
    qid = created(db, fn_body())
    db["state"]["answers"] = True
    r = cq.as_user(cq.HR).patch(f"/api/v1/questions/{qid}", json={"mode": "stdin"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "QUESTION_IN_USE"
    r = cq.as_user(cq.HR).patch(f"/api/v1/questions/{qid}", json={"marks": 3})
    assert r.status_code == 200  # other fields still editable


# --- the student's view (GET /attempt) -------------------------------------------------------

NOW = datetime.now(UTC)


class FakeSession:
    async def commit(self):
        pass


def get_attempt(monkeypatch, coding_rows):
    async def get_state(*_):
        return {
            "id": uuid.uuid4(),
            "test_id": uuid.uuid4(),
            "status": "in_progress",
            "deadline_at": NOW,
            "server_now": NOW,
            "tab_switches": 0,
            "remaining_sec_on_lock": None,
            "past_deadline": False,
        }

    async def coding(*_):
        return coding_rows

    async def none(*_):
        return []

    async def no_saved(*_):
        return {}

    monkeypatch.setattr(attempt_repo, "get_state", get_state)
    monkeypatch.setattr(attempt_repo, "coding_questions_for", coding)
    monkeypatch.setattr(attempt_repo, "questions_for", none)
    monkeypatch.setattr(attempt_repo, "saved_answers", no_saved)
    app.dependency_overrides[get_session] = lambda: FakeSession()
    try:
        token = auth_entry.issue_attempt_token(uuid.uuid4(), uuid.uuid4())
        r = TestClient(app).get("/api/v1/attempt", headers={"Authorization": f"Bearer {token}"})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200, r.text
    return r


def test_attempt_shows_mode_signature_starters_and_readable_samples_never_the_driver(
    monkeypatch,
):
    row = {
        "id": uuid.uuid4(),
        "body": "Two Sum\nReturn the indices.",
        "marks": 5,
        "mode": "function",
        "signature": SIG.model_dump_json(),  # jsonb as text, as asyncpg returns it
        "samples": [{"input": "[2,7]\n9\n", "expected": "[0,1]\n"}],
    }
    r = get_attempt(monkeypatch, [row])
    q = r.json()["questions"][0]
    assert q["mode"] == "function" and q["signature"] == TWO_SUM
    assert q["starter_code"] == starter_code(SIG)
    assert [x["key"] for x in q["languages"]] == list(FUNCTION_LANGUAGES)  # no SQL
    assert q["samples"] == [
        {"input": "[2,7]\n9\n", "expected": "[0,1]\n", "input_display": "nums = [2,7], target = 9"}
    ]
    for marker in ("drv", "driver", "#line", "redirect_stdout", "readFileSync"):
        assert marker not in r.text


def test_attempt_stdin_question_is_unchanged_apart_from_null_function_fields(monkeypatch):
    row = {
        "id": uuid.uuid4(),
        "body": "Sum\nRead n numbers.",
        "marks": 5,
        "mode": "stdin",
        "signature": None,
        "samples": [{"input": "2\n1 2\n", "expected": "3\n"}],
    }
    q = get_attempt(monkeypatch, [row]).json()["questions"][0]
    assert q["mode"] == "stdin" and q["signature"] is None and q["starter_code"] is None
    assert "sql" in [x["key"] for x in q["languages"]]
    assert q["samples"][0]["input_display"] == "2\n1 2\n"

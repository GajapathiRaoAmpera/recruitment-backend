"""SQL questions on a REAL Judge0 (SQLite 3.27.2 in the pinned image): the setup, the line
remap and the row comparison end to end, with expected rows from the reference query.

INTEGRATION test (skipped by default; run with `make test-judge0` after `make judge0-up`).
No LLM: the reply is the fixed one from test_sql_mode.py. Unit-level versions of the same
scenarios (with a local sqlite3 stand-in) are in test_sql_mode.py.
"""

import asyncio

import httpx
import pytest

from app.execution.coding_questions import generate as gen
from app.execution.judge0 import Judge0Client
from app.execution.judging import Verdict
from app.execution.scoring import CaseSpec, run_samples, submit
from app.execution.sql_mode.runner import SqlSetup
from tests.execution.test_sql_mode import (
    NO_ROWID_KEY,
    REFERENCE,
    SAME_ROWS_OTHER_ORDER,
    WRONG,
    question,
    reply,
)

pytestmark = pytest.mark.integration


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


@pytest.fixture(scope="module")
def cases(judge0) -> list[CaseSpec]:
    out = asyncio.run(gen.sql_expected_cases(judge0, question()))
    assert not isinstance(out, tuple), out
    return [CaseSpec(c.input, c.expected, c.is_sample) for c in out]


def score(judge0, cases, query: str, order_matters: bool = True):
    setup = SqlSetup(question().sql_schema, order_matters)
    return asyncio.run(submit(judge0, "sql", query, cases, setup))


def test_expected_rows_come_from_the_reference_on_18_distinct_datasets(cases):
    assert len(cases) == 18 and len({c.input for c in cases}) == 18
    assert [c.is_sample for c in cases] == [True] * 3 + [False] * 15
    assert cases[0].expected == "Eng|4\nSales|3\nHR|1\nOps|1\n"
    assert len({c.expected for c in cases}) > 1


def test_correct_query_passes_and_wrong_query_fails(judge0, cases):
    assert score(judge0, cases, REFERENCE).passed == 18
    assert score(judge0, cases, WRONG).passed < 18


def test_row_order_counts_only_when_order_matters(judge0, cases):
    assert score(judge0, cases, SAME_ROWS_OTHER_ORDER, order_matters=False).passed == 18
    assert score(judge0, cases, SAME_ROWS_OTHER_ORDER, order_matters=True).passed < 18


def test_sql_error_is_a_runtime_error_on_the_students_line(judge0, cases):
    setup = SqlSetup(question().sql_schema, True)
    query = "SELECT 1;\n\nSELECT name\n  FROM nope;"
    [first, *_] = asyncio.run(run_samples(judge0, "sql", query, cases, setup))
    assert first.verdict is Verdict.RUNTIME_ERROR
    assert first.detail == "Error: near line 3: no such table: nope\n"
    syntax = asyncio.run(run_samples(judge0, "sql", "SELEC 1;", cases, setup))[0]
    assert syntax.detail == 'Error: near line 1: near "SELEC": syntax error\n'


def test_drop_and_delete_in_a_query_only_affect_its_own_run(judge0, cases):
    query = "DROP TABLE employees;\nDELETE FROM departments;\nSELECT 1;"
    assert score(judge0, cases, query).passed == 0  # every run broke only its own copy
    query = REFERENCE + "\nDROP TABLE employees;\nDELETE FROM departments;"
    assert score(judge0, cases, query).passed == 18  # ...so later runs still see all data


def test_unbroken_ties_are_caught_by_the_insertion_order_check(judge0):
    q = gen.GeneratedSqlQuestion.model_validate(
        reply(
            reference_query="SELECT name, salary FROM employees ORDER BY salary DESC;",
            schema=NO_ROWID_KEY,
        )
    )
    reason, _ = asyncio.run(gen.sql_expected_cases(judge0, q))
    assert "insertion order" in reason

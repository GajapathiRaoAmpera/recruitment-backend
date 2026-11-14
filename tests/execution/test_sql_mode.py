"""SQL questions without Judge0 or an LLM: script checks, dataset derivation, generation,
HR edits, Run / Submit and the student view.

The LLM is a script of replies. Judge0 is `ShellJudge0`: it runs each source through Python's
sqlite3 the way the sqlite3 shell does (fresh database per run, our setup dot-commands, keep
going after an error, "Error: near line N:", exit 1). The same scenarios on a real Judge0:
test_sql_mode_judge0.py (integration).
"""

import asyncio
import sqlite3
import uuid

import pytest
from pydantic import ValidationError

from app.attempts import state_repo as attempt_repo
from app.execution.coding_questions import generate as gen
from app.execution.judge0 import ACCEPTED, RunResult
from app.execution.scoring import CaseSpec
from app.execution.sql_mode import datasets
from app.execution.sql_mode.runner import (
    SETTINGS,
    SqlSetup,
    load_setup,
    remap_lines,
    rows_match,
)
from app.execution.sql_mode.script import check_query, parse_dataset, parse_schema
from tests.execution import test_attempt_api as ta
from tests.execution import test_function_mode as tf
from tests.execution import test_generate_coding as tg

env = ta.env  # run / submit fixture (fake DB + Judge0)
db = tg.db  # generation fixture (fake coding-question repo)

SCHEMA = (
    "CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT);\n"
    "CREATE TABLE employees (\n  id INTEGER PRIMARY KEY,\n  name TEXT,\n  dept_id INTEGER,\n"
    "  salary REAL\n);"
)
DATASET = (
    "INSERT INTO departments VALUES (1, 'Eng'), (2, 'Sales'), (3, 'HR'), (4, 'Ops');\n"
    "INSERT INTO employees (id, name, dept_id, salary) VALUES (1, 'Asha', 1, 100), "
    "(2, 'Ravi', 1, 90), (3, 'Meena', 2, 70), (4, 'John', 2, 70), (5, 'Li', 3, NULL), "
    "(6, 'Sam', 1, 120), (7, 'Ana', 2, 65), (8, 'Kai', NULL, 50), (9, 'Bo', 4, 80), "
    "(10, 'O''Neil', 1, 95);"
)
REFERENCE = (
    "SELECT d.name, COUNT(e.id) FROM departments d LEFT JOIN employees e ON e.dept_id = d.id "
    "GROUP BY d.id ORDER BY COUNT(e.id) DESC, d.name;"
)
SAME_ROWS_OTHER_ORDER = (
    "SELECT d.name, COUNT(e.id) FROM departments d LEFT JOIN employees e ON e.dept_id = d.id "
    "GROUP BY d.id ORDER BY d.name DESC;"
)
WRONG = (  # an inner join drops departments without employees
    "SELECT d.name, COUNT(*) FROM departments d JOIN employees e ON e.dept_id = d.id "
    "GROUP BY d.id ORDER BY COUNT(*) DESC, d.name;"
)


def reply(**over) -> dict:
    return {
        "title": "Department Head Count",
        "statement": "Return each department's name and how many employees it has, "
        "ordered by the count (highest first), then by name.",
        "schema": SCHEMA,
        "dataset": DATASET,
        "reference_query": REFERENCE,
        "order_matters": True,
        **over,
    }


# --- a sqlite3-shell stand-in for Judge0 -----------------------------------------------------


def _split(buf: str) -> list[str]:
    out, start = [], 0
    for i, ch in enumerate(buf):
        if ch == ";" and sqlite3.complete_statement(buf[start : i + 1]):
            out.append(buf[start : i + 1])
            start = i + 1
    return [s for s in out if s.strip(" \n;")]


def shell(source: str) -> RunResult:
    con = sqlite3.connect(":memory:")  # .open :memory: - every run starts empty
    out, err, buf, start = [], [], "", 1
    for n, line in enumerate(source.split("\n"), start=1):
        if not buf.strip():
            if line.lstrip().startswith("."):
                continue  # only our fixed settings in these tests
            start = n
        buf += line + "\n"
        if not sqlite3.complete_statement(buf):
            continue
        try:
            for stmt in _split(buf):
                for row in con.execute(stmt).fetchall():
                    out.append("|".join("NULL" if v is None else str(v) for v in row))
        except sqlite3.Error as e:
            err.append(f"Error: near line {start}: {e}")
        buf = ""
    stdout = "".join(line + "\n" for line in out)
    if err:
        stderr = "".join(e + "\n" for e in err)
        return RunResult(11, "Runtime Error (NZEC)", stdout, stderr, "", "", 0.01, 900, 1)
    return RunResult(ACCEPTED, "Accepted", stdout, "", "", "", 0.01, 900, 0)


class ShellJudge0:
    def __init__(self):
        self.sources: list[str] = []
        self.calls = []

    async def run_sources(self, language, sources, stdins=None):
        assert language == "sql" and stdins is None
        self.sources += sources
        self.calls.append(list(sources))
        return [shell(s) for s in sources]

    async def run_many(self, language, source, stdins):
        raise AssertionError("SQL questions build one source per dataset")


def question(**over) -> gen.GeneratedSqlQuestion:
    return gen.GeneratedSqlQuestion.model_validate(reply(**over))


def expected_cases(q: gen.GeneratedSqlQuestion) -> list[CaseSpec]:
    setup = SqlSetup(q.sql_schema, q.order_matters)
    return [
        CaseSpec(d, shell(setup.build_source(d, REFERENCE)).stdout, i < 3)
        for i, d in enumerate(q.inputs)
    ]


# --- strict checks of what the LLM writes ----------------------------------------------------


@pytest.mark.parametrize(
    "over,why",
    [
        ({"schema": SCHEMA + "\nINSERT INTO departments VALUES (9, 'X');"}, "CREATE TABLE"),
        ({"schema": SCHEMA + "\nDROP TABLE departments;"}, "CREATE TABLE"),
        ({"schema": "CREATE VIEW v AS SELECT 1;"}, "CREATE TABLE"),
        ({"schema": "CREATE TABLE t AS SELECT 1 AS a;"}, "schema"),
        ({"schema": "CREATE TEMP TABLE t (a INTEGER);"}, "CREATE TABLE"),
        ({"schema": "PRAGMA foreign_keys = ON;\n" + SCHEMA}, "PRAGMA"),
        ({"schema": "ATTACH 'x.db' AS x;\n" + SCHEMA}, "ATTACH"),
        ({"schema": ".shell ls\n" + SCHEMA}, "dot-commands"),
        ({"schema": SCHEMA + "\nCREATE TABLE a (x INT);\nCREATE TABLE b (y INT);"}, "1 to 3"),
        ({"dataset": DATASET + "\nUPDATE employees SET salary = 1;"}, "INSERT INTO"),
        ({"dataset": DATASET + "\nDELETE FROM employees;"}, "INSERT INTO"),
        ({"dataset": "INSERT INTO employees SELECT * FROM employees;"}, "VALUES"),
        ({"dataset": DATASET.replace("100)", "random())")}, "literals"),
        ({"dataset": DATASET.replace("(2, 'Sales')", "(2)")}, "values for"),
        ({"dataset": DATASET.replace("(id, name, dept_id, salary)", "(id, name)")}, "every column"),
        ({"dataset": DATASET + "\nINSERT INTO nope VALUES (1);"}, "does not create"),
        ({"dataset": DATASET.replace("'Eng'", "'E|ng'")}, "'|'"),
        ({"dataset": DATASET + "\n.read evil.sql"}, "dot-commands"),
        ({"dataset": DATASET.replace("'Eng'", "'Eng")}, "unterminated"),
        ({"reference_query": REFERENCE + " SELECT 1;"}, "exactly one statement"),
        ({"reference_query": "DELETE FROM employees;"}, "SELECT"),
        ({"reference_query": "WITH x AS (SELECT 1) DELETE FROM employees;"}, "only read"),
        ({"reference_query": "SELECT * FROM employees; DROP TABLE employees;"}, "exactly one"),
        ({"reference_query": "PRAGMA table_info(employees);"}, "PRAGMA"),
        ({"reference_query": "ATTACH 'x' AS y;"}, "ATTACH"),
        ({"reference_query": ".tables"}, "dot-commands"),
        ({"reference_query": "SELECT name FROM employees ORDER BY random();"}, "same rows"),
        ({"reference_query": "SELECT date('now');"}, "same rows"),
        ({"reference_query": "SELECT name FROM employees;"}, "ORDER BY"),  # order_matters
        ({"reference_query": "SELECT " + "1 + " * 600 + "1;"}, "longer than"),
        ({"order_matters": "yes"}, "order_matters"),
        (
            {
                "dataset": "INSERT INTO departments VALUES (1, 'Eng');\n"
                "INSERT INTO employees VALUES (1, 'A', 1, 1);"
            },
            "too few rows",
        ),
    ],
)
def test_validation_rejects_anything_but_create_insert_select(over, why):
    with pytest.raises(ValidationError) as e:
        question(**over)
    assert why in str(e.value)


def test_a_valid_reply_is_normalised_one_row_per_insert_with_every_column():
    q = question(chatter="ignored")
    assert q.mode == "sql" and q.reference_language == "sql" and q.signature is None
    assert q.sql_schema.count("CREATE TABLE") == 2 and q.sql_schema.endswith(");")
    assert q.reference_solution == REFERENCE
    base = q.inputs[0].splitlines()
    assert len(base) == 14 and base[0] == "INSERT INTO departments (id, name) VALUES (1, 'Eng');"
    assert (
        "INSERT INTO employees (id, name, dept_id, salary) VALUES (10, 'O''Neil', 1, 95);" in base
    )


def test_check_query_accepts_with_and_comments():
    q = "-- head\nWITH c AS (SELECT 1 AS a) SELECT a FROM c /* end */"
    assert check_query(q, order_matters=False) == "WITH c AS (SELECT 1 AS a) SELECT a FROM c;"


# --- dataset derivation ----------------------------------------------------------------------


def test_derivation_is_deterministic_and_gives_18_distinct_datasets_from_the_base_rows():
    schema = parse_schema(SCHEMA)
    rows = parse_dataset(DATASET, schema)
    first, again = datasets.derive(schema, rows), datasets.derive(schema, rows)
    assert first == again and len(first) == 18 and len(set(first)) == 18
    assert first == question().inputs
    assert first[0] == datasets.render(rows)  # sample 1 = the full base data, as written
    base = set(first[0].splitlines())
    sizes = set()
    for d in first[1:]:
        lines = d.splitlines()
        assert set(lines) <= base and len(set(lines)) == len(lines)  # only base rows, no dupes
        tables = datasets.tables(schema, d)
        assert all(t["rows"] for t in tables.values())  # every table keeps a row
        sizes.add(len(lines))
    assert len(base) in sizes and min(sizes) < len(base)  # shuffles and subsets both appear


def test_a_dataset_turns_into_tables_for_the_student_view():
    schema = parse_schema(SCHEMA)
    t = datasets.tables(schema, question().inputs[0])
    assert t["departments"] == {
        "columns": ["id", "name"],
        "rows": [[1, "Eng"], [2, "Sales"], [3, "HR"], [4, "Ops"]],
    }
    assert t["employees"]["columns"] == ["id", "name", "dept_id", "salary"]
    assert [5, "Li", 3, None] in t["employees"]["rows"]
    assert [10, "O'Neil", 1, 95] in t["employees"]["rows"]


# --- comparing and line numbers --------------------------------------------------------------


def test_rows_compare_ignoring_trailing_whitespace_and_optionally_order():
    assert rows_match("a|1  \nb|2\n\n", "a|1\nb|2", order_matters=True)
    assert not rows_match("b|2\na|1\n", "a|1\nb|2\n", order_matters=True)
    assert rows_match("b|2\na|1\n", "a|1\nb|2\n", order_matters=False)
    assert not rows_match("a|1\na|1\n", "a|1\nb|2\n", order_matters=False)  # rows, not sets
    assert rows_match("", "\n", order_matters=False)


def test_error_lines_are_shifted_to_the_students_query():
    assert remap_lines("Error: near line 23: no such column: x\n", 20) == (
        "Error: near line 3: no such column: x\n"
    )
    assert remap_lines("Parse error near line 21: x", 20) == "Parse error near line 1: x"


def test_the_source_is_settings_schema_dataset_then_the_query():
    q = question()
    setup = SqlSetup(q.sql_schema, True)
    src = setup.build_source(q.inputs[0], "SELECT 1")
    assert src.startswith(SETTINGS) and src.startswith(".open :memory:\n")
    assert src.endswith("\nSELECT 1\n;\n")
    assert src.split("\n")[setup.line_offset(q.inputs[0])] == "SELECT 1"  # line 1 of the query


# --- generation (POST /questions/generate, mode "sql") ---------------------------------------


def test_sql_question_is_generated_with_expected_rows_from_the_reference_query(db):
    llm = tg.FakeLlm({0: [reply()]})
    judge0 = ShellJudge0()
    c, _ = tg.use(llm, judge0)
    r = tg.post(c, mode="sql", topic="joins")
    assert r.status_code == 201, r.text
    [q] = r.json()["created"]
    assert q["mode"] == "sql" and q["order_matters"] is True and q["signature"] is None
    assert q["schema"] == question().sql_schema and q["approved"] is False
    assert q["reference_solution"] == REFERENCE and q["reference_language"] == "sql"
    cases = q["test_cases"]
    assert [x["is_sample"] for x in cases] == [True] * 3 + [False] * 15
    assert [x["input"] for x in cases] == question().inputs
    assert cases[0]["expected"] == "Eng|4\nSales|3\nHR|1\nOps|1\n"
    for case in cases:  # every expected output is the reference query's rows on that dataset
        src = SqlSetup(q["schema"], True).build_source(case["input"], REFERENCE)
        assert case["expected"] == shell(src).stdout
    # 18 datasets + the same 18 with rows in reverse order, one source each, in 2 batches
    assert len(judge0.sources) == 36 and [len(b) for b in judge0.calls] == [20, 16]
    assert all(s.startswith(SETTINGS) for s in judge0.sources)
    assert llm.prompts[0][0].startswith("Difficulty: MEDIUM: a JOIN")
    saved = db.questions[next(iter(db.questions))]
    assert saved["sql_schema"] == q["schema"] and saved["sql_order_matters"] is True
    assert db.audits[0][3]["reference_language"] == "sql"


def test_sql_prompt_is_the_short_one():
    assert len(gen.SQL_SYSTEM_PROMPT) < len(gen.SYSTEM_PROMPT)
    assert "dataset" in gen.SQL_SYSTEM_PROMPT and "hidden_inputs" not in gen.SQL_SYSTEM_PROMPT


def test_a_failing_reference_query_is_retried_with_the_error_then_used(db):
    broken = reply(reference_query=REFERENCE.replace("e.dept_id", "e.dept"))
    llm = tg.FakeLlm({0: [broken, reply()]})
    c, _ = tg.use(llm, ShellJudge0())
    r = tg.post(c, mode="sql")
    assert r.status_code == 201 and len(r.json()["created"]) == 1
    assert "no such column: e.dept" in llm.prompts[0][1] and "near line 1" in llm.prompts[0][1]


NO_ROWID_KEY = SCHEMA.replace("  id INTEGER PRIMARY KEY,", "  id INTEGER,")


@pytest.mark.parametrize(
    "query,order,schema,why",
    [
        ("SELECT COUNT(*) > 0 FROM employees;", False, SCHEMA, "same result"),
        ("SELECT name FROM employees WHERE salary > 1000;", False, SCHEMA, "empty result"),
        # without an INTEGER PRIMARY KEY, SQLite returns rows in insertion order
        ("SELECT name FROM employees LIMIT 3;", False, NO_ROWID_KEY, "insertion order"),
        (  # ties (two salaries of 70) keep insertion order: ORDER BY doesn't decide it
            "SELECT name, salary FROM employees ORDER BY salary DESC;",
            True,
            NO_ROWID_KEY,
            "insertion order",
        ),
    ],
)
def test_useless_or_order_dependent_reference_queries_fail(db, query, order, schema, why):
    bad = reply(reference_query=query, order_matters=order, schema=schema)
    c, _ = tg.use(tg.FakeLlm({0: [bad, bad]}), ShellJudge0())
    r = tg.post(c, mode="sql")
    assert r.status_code == 502 and r.json()["error"]["code"] == "AI_BAD_OUTPUT"
    assert why in r.json()["error"]["details"]["failed"][0]["reason"] and db.questions == {}


def test_a_tie_broken_order_passes_the_insertion_order_check(db):
    tie_broken = "SELECT name, salary FROM employees ORDER BY salary DESC, name;"
    ok = reply(reference_query=tie_broken, schema=NO_ROWID_KEY)
    c, _ = tg.use(tg.FakeLlm({0: [ok]}), ShellJudge0())
    assert tg.post(c, mode="sql").status_code == 201


def test_invalid_sql_reply_never_reaches_judge0(db):
    bad = reply(schema="DROP TABLE x;")
    j = ShellJudge0()
    c, _ = tg.use(tg.FakeLlm({0: [bad, bad]}), j)
    r = tg.post(c, mode="sql")
    assert r.status_code == 502 and "schema" in r.text and j.sources == []


# --- Run / Submit ----------------------------------------------------------------------------


def sql_env(env, order_matters=True):
    q = question()
    env["sql"] = {"sql_schema": q.sql_schema, "sql_order_matters": order_matters}
    env["cases"] = expected_cases(q)
    env["judge0"] = ShellJudge0()
    return env


def body(code: str, language: str = "sql") -> dict:
    return {**ta.BODY, "code": code, "language": language}


def test_a_correct_query_passes_every_dataset_and_only_the_query_is_saved(env):
    sql_env(env)
    r = ta.post("submit-code", body(REFERENCE))
    assert r.status_code == 200 and r.json() == {"passed": 18, "total": 18}
    assert env["saved"] == [(ta.CTX.attempt_id, ta.QID, REFERENCE, "sql", 18, 18)]
    assert len(env["judge0"].sources) == 18


def test_a_wrong_query_fails(env):
    sql_env(env)
    r = ta.post("submit-code", body(WRONG))
    assert r.status_code == 200 and r.json()["passed"] < 18


@pytest.mark.parametrize("order_matters,passed", [(False, 18), (True, None)])
def test_row_order_counts_only_when_order_matters(env, order_matters, passed):
    sql_env(env, order_matters)
    got = ta.post("submit-code", body(SAME_ROWS_OTHER_ORDER)).json()["passed"]
    assert got == 18 if passed else got < 18


def test_run_shows_three_sample_results_and_never_the_setup(env):
    sql_env(env)
    r = ta.post("run", body(REFERENCE))
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["status"] for x in results] == ["passed"] * 3
    assert results[0]["stdout"] == "Eng|4\nSales|3\nHR|1\nOps|1\n"
    assert env["loads"] == [True]  # hidden datasets are not even loaded
    for secret in ("INSERT INTO", ".open", "CREATE TABLE", "Asha"):
        assert secret not in r.text


def test_a_sql_error_is_a_runtime_error_on_the_students_line(env):
    sql_env(env)
    r = ta.post("run", body("SELECT 1;\n\nSELECT name\n  FROM nope;"))
    [first, *_] = r.json()["results"]
    assert first["status"] == "runtime_error" and first["passed"] is False
    assert first["stderr"] == "Error: near line 3: no such table: nope\n"
    syntax = ta.post("run", body("SELEC name FROM employees;")).json()["results"][0]
    assert syntax["status"] == "runtime_error"
    assert syntax["stderr"] == 'Error: near line 1: near "SELEC": syntax error\n'


def test_drop_table_in_a_query_only_affects_its_own_run(env):
    sql_env(env)
    r = ta.post(
        "submit-code", body(REFERENCE + "\nDROP TABLE employees;\nDELETE FROM departments;")
    )
    assert r.json() == {"passed": 18, "total": 18}
    assert all(s.startswith(".open :memory:\n") for s in env["judge0"].sources)


@pytest.mark.parametrize("path", ["run", "submit-code"])
def test_only_sql_is_allowed_for_a_sql_question(env, path):
    sql_env(env)
    r = ta.post(path, body("print(1)", "python"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "LANGUAGE_NOT_ALLOWED"
    assert r.json()["error"]["details"]["allowed"] == ["sql"] and env["judge0"].sources == []


def test_stdin_question_with_sql_language_still_runs_the_code_as_is(env):
    """Unchanged stdin behaviour (reported, not changed): SQL is offered and the code is sent
    to Judge0 unchanged, with each case's input as stdin (which SQLite ignores)."""
    r = ta.post("run", body("SELECT 1;"))
    assert r.status_code == 200 and env["judge0"].sources == ["SELECT 1;"]
    assert env["judge0"].calls == [["s0", "s1", "s2"]]


# --- the student view (GET /attempt) ---------------------------------------------------------


def test_attempt_shows_schema_and_sample_tables_never_hidden_data_or_reference(monkeypatch):
    q = question()
    cases = expected_cases(q)
    row = {
        "id": uuid.uuid4(),
        "body": q.body,
        "marks": 5,
        "mode": "sql",
        "signature": None,
        "sql_schema": q.sql_schema,
        "sql_order_matters": False,
        "samples": [{"input": c.input, "expected": c.expected} for c in cases[:3]],
    }
    r = tf.get_attempt(monkeypatch, [row])
    out = r.json()["questions"][0]
    assert out["mode"] == "sql" and out["schema"] == q.sql_schema
    assert out["order_matters"] is False and out["starter_code"] is None
    assert out["languages"] == [{"key": "sql", "name": "SQL (SQLite 3)"}]
    assert len(out["samples"]) == 3
    first = out["samples"][0]
    assert first["input"] == "" and first["input_display"] is None
    assert first["tables"] == datasets.tables(parse_schema(q.sql_schema), cases[0].input)
    assert first["expected_rows"] == [["Eng", "4"], ["Sales", "3"], ["HR", "1"], ["Ops", "1"]]
    assert "INSERT" not in r.text and REFERENCE not in r.text and "COUNT(e.id)" not in r.text
    hidden_only = set(cases[3].input.splitlines()) - set(
        line for c in cases[:3] for line in c.input.splitlines()
    )
    assert all(line not in r.text for line in hidden_only)


class RecordingSession:
    def __init__(self, results):
        self.sql, self.results = [], list(results)

    async def execute(self, stmt, params=None):
        self.sql.append(str(stmt))
        rows = self.results.pop(0)

        class Result:
            def mappings(self):
                return rows

        return Result()


def test_attempt_query_selects_sample_cases_and_no_reference_columns():
    qid = uuid.uuid4()
    session = RecordingSession(
        [
            [
                {
                    "id": qid,
                    "body": "t",
                    "marks": 1,
                    "mode": "sql",
                    "signature": None,
                    "sql_schema": "CREATE TABLE t (a INTEGER);",
                    "sql_order_matters": True,
                }
            ],
            [],
        ]
    )
    rows = asyncio.run(attempt_repo.coding_questions_for(session, uuid.uuid4()))
    assert rows[0]["sql_schema"].startswith("CREATE TABLE") and rows[0]["samples"] == []
    assert "and is_sample" in session.sql[1]
    assert all("reference" not in s for s in session.sql)


def test_stdin_and_function_samples_keep_their_exact_shape(monkeypatch):
    row = {
        "id": uuid.uuid4(),
        "body": "Sum\nRead n numbers.",
        "marks": 5,
        "mode": "stdin",
        "signature": None,
        "samples": [{"input": "2\n1 2\n", "expected": "3\n"}],
    }
    q = tf.get_attempt(monkeypatch, [row]).json()["questions"][0]
    assert q["samples"] == [{"input": "2\n1 2\n", "expected": "3\n", "input_display": "2\n1 2\n"}]
    assert q["schema"] is None and q["order_matters"] is None


def test_sql_setup_row_for_other_modes_is_none():
    assert load_setup(None) is None
    assert load_setup(
        {"sql_schema": "CREATE TABLE t (a INTEGER);", "sql_order_matters": False}
    ) == (SqlSetup("CREATE TABLE t (a INTEGER);", False))

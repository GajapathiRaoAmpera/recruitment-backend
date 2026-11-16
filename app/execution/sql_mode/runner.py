"""One Judge0 run of a SQL question = the hidden setup + the student's (or reference) query.

Judge0's SQLite language runs the *source* through the sqlite3 shell and ignores stdin, so
each dataset gets its own source (Judge0Client.run_sources):

    .open :memory:          <- the sandbox can't write Judge0's db file; a fresh in-memory DB
    .headers off            <- fixed output settings: no column names, "|" between values,
    .mode list                 NULL printed as NULL (an empty string prints as nothing)
    .separator |
    .nullvalue NULL
    <schema>                <- CREATE TABLE statements
    <dataset>               <- one INSERT per row
    <query>                 <- the student's text, unchanged
    ;                       <- ends a last statement written without ';'

The sqlite3 shell keeps going after an error and exits 1, which Judge0 reports as a runtime
error with "Error: near line N: ..." on stderr; N counts from the top of the source, so it is
shifted back to the student's own line numbers. build_source() output goes to Judge0 only:
never into a response, DB row or log.

Comparing: trailing whitespace is ignored (as for every question) and column names never
print; when row order does not matter, both outputs' rows are sorted before comparing.
"""

import re
from dataclasses import dataclass, replace

from app.core.errors import AppError
from app.execution.judge0 import RunResult
from app.execution.judging import CaseJudgement, judge, normalize_output

SQL_LANGUAGES = ("sql",)
SETTINGS = ".open :memory:\n.headers off\n.mode list\n.separator |\n.nullvalue NULL\n"
SEPARATOR = "|"

_ERROR_LINE = re.compile(r"\bnear line (\d+)")


@dataclass(frozen=True)
class SqlSetup:
    """What a SQL question adds to its test cases (each case's input is one dataset)."""

    schema: str
    order_matters: bool

    def _prefix(self, dataset: str) -> str:
        data = dataset if dataset.endswith("\n") or not dataset else dataset + "\n"
        return f"{SETTINGS}{self.schema.rstrip()}\n{data}"

    def build_source(self, dataset: str, query: str) -> str:
        """Setup + query for one dataset. Line N of `query` is line N + line_offset()."""
        return f"{self._prefix(dataset)}{query}\n;\n"

    def line_offset(self, dataset: str) -> int:
        return self._prefix(dataset).count("\n")

    def judge(self, dataset: str, result: RunResult, expected: str) -> CaseJudgement:
        """Same verdicts as every question (passed, wrong_answer, runtime_error for a SQL
        error, time_limit), with the student's line numbers and the row comparison."""
        fixed = replace(result, stderr=remap_lines(result.stderr, self.line_offset(dataset)))
        return judge("sql", fixed, expected, match=self.rows_match)

    def rows_match(self, actual: str, expected: str) -> bool:
        return rows_match(actual, expected, order_matters=self.order_matters)


def load_setup(row) -> SqlSetup | None:
    """From a DB row with sql_schema / sql_order_matters (None for other modes)."""
    if row is None or row["sql_schema"] is None:
        return None
    return SqlSetup(schema=row["sql_schema"], order_matters=bool(row["sql_order_matters"]))


def remap_lines(stderr: str, offset: int) -> str:
    """ "Error: near line 23: ..." -> the line in the student's query."""

    def shift(m: re.Match) -> str:
        n = int(m.group(1)) - offset
        return f"near line {n}" if n >= 1 else m.group(0)

    return _ERROR_LINE.sub(shift, stderr)


def rows_match(actual: str, expected: str, *, order_matters: bool) -> bool:
    a, e = normalize_output(actual), normalize_output(expected)
    if order_matters:
        return a == e
    return sorted(a.split("\n")) == sorted(e.split("\n"))


def output_rows(stdout: str) -> list[list[str]]:
    """The rows of a query's output, for display ("1|Asha|NULL" -> ["1", "Asha", "NULL"])."""
    text = normalize_output(stdout)
    return [line.split(SEPARATOR) for line in text.split("\n")] if text else []


def check_language(language: str) -> None:
    if language not in SQL_LANGUAGES:
        raise AppError(
            422,
            "LANGUAGE_NOT_ALLOWED",
            f"{language} can't be used for this question. Choose one of: "
            f"{', '.join(SQL_LANGUAGES)}",
            {"allowed": list(SQL_LANGUAGES)},
        )

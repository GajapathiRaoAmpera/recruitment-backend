"""repo.save_submission: the SQL that turns a pass count into answers.marks. The database is a
recorder here; tests/execution/test_end_to_end.py runs the same SQL on a real Postgres."""

import uuid

import pytest

from app.execution import repo

A, Q = uuid.uuid4(), uuid.uuid4()
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class Recorder:
    def __init__(self, status):
        self.status = status
        self.sql: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        self.sql.append((str(stmt), params or {}))
        return Result(self.status)


async def save(session):
    return await repo.save_submission(session, A, Q, "print(1)", "python", 12, 18)


@pytest.mark.parametrize("status", ["in_progress", "locked"])
async def test_saves_code_counts_and_marks_while_the_attempt_is_open(status):
    s = Recorder(status)
    assert await save(s) is True
    lock, (insert, params) = s.sql
    assert "for update" in lock[0] and lock[1] == {"a": A}
    assert "marks = excluded.marks" in insert  # a re-submit replaces the old mark too
    assert "round(q.marks * cast(:passed as int) / nullif(cast(:total as int), 0), 2)" in insert
    assert params == {
        "a": A,
        "q": Q,
        "code": "print(1)",
        "lang": "python",
        "passed": 12,
        "total": 18,
    }


@pytest.mark.parametrize("status", ["submitted", "expired", "not_started", None])
async def test_saves_nothing_once_the_attempt_is_final(status):
    s = Recorder(status)
    assert await save(s) is False
    assert len(s.sql) == 1  # only the lock; no insert

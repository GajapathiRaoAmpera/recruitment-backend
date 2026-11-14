"""HR edits of a SQL question: body, difficulty, tags, marks and approval change as for every
question; mode, schema and datasets are read-only. SQL questions can't be created by hand.
DB is faked (test_coding_questions.db); no Judge0, no LLM."""

import uuid

from tests.execution import test_coding_questions as cq
from tests.execution import test_function_mode as tf
from tests.execution.test_sql_mode import REFERENCE, expected_cases, question

db = cq.db


def sql_row(owner) -> dict:
    q = question()
    row = dict(
        id=uuid.uuid4(),
        body=q.body,
        difficulty="medium",
        tags=[],
        marks=5,
        approved=False,
        created_by=owner,
        created_at=cq.NOW,
        reference_solution=REFERENCE,
        reference_language="sql",
        mode="sql",
        signature=None,
        sql_schema=q.sql_schema,
        sql_order_matters=True,
    )
    return row


def test_hr_edits_body_marks_and_approval_but_not_schema_or_datasets(db):
    row = sql_row(cq.HR.id)
    db["questions"][row["id"]] = row
    db["cases"][row["id"]] = [
        dict(id=uuid.uuid4(), position=i, input=c.input, expected=c.expected, is_sample=c.is_sample)
        for i, c in enumerate(expected_cases(question()))
    ]
    client = cq.as_user(cq.HR)
    url = f"/api/v1/questions/{row['id']}"
    ok = client.patch(url, json={"body": "New title\nNew text", "marks": 7, "approved": True})
    assert ok.status_code == 200, ok.text
    out = ok.json()
    assert out["body"] == "New title\nNew text" and out["approved"] is True
    assert (
        out["mode"] == "sql" and out["schema"] == row["sql_schema"] and len(out["test_cases"]) == 18
    )
    for patch in (
        {"test_cases": cq.cases()},
        {"mode": "stdin"},
        {"mode": "function", "signature": tf.TWO_SUM},
    ):
        r = client.patch(url, json=patch)
        assert r.status_code == 422 and "SQL question" in r.text, patch
    assert db["questions"][row["id"]]["mode"] == "sql"


def test_sql_questions_cannot_be_created_by_hand(db):
    r = cq.create(mode="sql")
    assert r.status_code == 422 and db["questions"] == {}

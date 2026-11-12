"""The question-bank repo's SQL shape (no DB: a recording session captures each statement)."""

import asyncio
import json
import uuid

import pytest

from app.qbank import repo
from app.qbank.parser import ParsedQuestion


class Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def one(self):
        assert len(self._rows) == 1
        return self._rows[0]

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0]["n"]


class RecordingSession:
    def __init__(self, *results):
        self.calls: list[tuple[str, dict]] = []
        self._results = list(results)

    async def execute(self, stmt, params=None):
        self.calls.append((" ".join(str(stmt).split()), params or {}))
        return Result(self._results.pop(0) if self._results else [])


def run(coro):
    return asyncio.run(coro)


SID = uuid.uuid4()

PARSED = [
    ParsedQuestion(
        subject="Operating Systems",
        number=7,
        level="advanced",
        topic="Scheduling",
        text="Which policy can starve a long job?",
        options=("FCFS", "SJF", "Round robin", "None"),
        answer=1,
        warnings=("Operating Systems #7: option B wrapped oddly",),
    ),
    ParsedQuestion(
        subject="Computer Networks",
        number=1,
        level="easy",
        topic="Layers",
        text="Which layer routes packets? x & (x - 1) O(n^2)",
        options=("Network", "Link", "Transport", "Session"),
        answer=0,
    ),
]


def test_question_rows_from_parsed():
    rows = repo.question_rows(PARSED)
    assert rows[0] == {
        "subject": "Operating Systems",
        "number": 7,
        "level": "advanced",
        "topic": "Scheduling",
        "text": "Which policy can starve a long job?",
        "options": ["FCFS", "SJF", "Round robin", "None"],
        "answer": 1,
        "warnings": ["Operating Systems #7: option B wrapped oddly"],
    }
    assert rows[1]["warnings"] == []


def test_insert_questions_casts_enum_and_jsonb():
    session = RecordingSession([{"id": uuid.uuid4()}, {"id": uuid.uuid4()}])
    n = run(repo.insert_questions(session, SID, repo.question_rows(PARSED)))
    assert n == 2
    [(sql, params)] = session.calls
    assert "insert into questions" in sql
    assert "cast('mcq' as question_kind)" in sql
    assert "cast(r.level as difficulty)" in sql
    assert "jsonb_to_recordset(cast(:rows as jsonb))" in sql
    assert "r.options" in sql and "options jsonb" in sql and "answer jsonb" in sql
    assert "from question_sources s" in sql and "where s.id = :source_id" in sql
    assert "s.uploaded_by" in sql  # created_by is the uploader
    assert "false" in sql  # never auto-approved
    assert params["source_id"] == SID
    payload = json.loads(params["rows"])
    assert payload[0]["options"] == ["FCFS", "SJF", "Round robin", "None"]
    assert payload[0]["answer"] == 1 and payload[0]["level"] == "advanced"
    assert payload[1]["text"].endswith("x & (x - 1) O(n^2)")  # bound, never inlined


def test_insert_questions_empty_is_noop():
    session = RecordingSession()
    assert run(repo.insert_questions(session, SID, [])) == 0
    assert session.calls == []


def test_list_questions_filters_and_paginates():
    q = {"id": uuid.uuid4(), "subject": "Operating Systems"}
    session = RecordingSession([{"n": 41}], [q])
    rows, total = run(
        repo.list_questions(
            session,
            SID,
            subject="Operating Systems",
            level="medium",
            approved=False,
            limit=20,
            offset=40,
        )
    )
    assert total == 41 and rows == [q]
    (count_sql, count_params), (sql, params) = session.calls
    for s in (count_sql, sql):
        assert "q.source_id = :source_id" in s
        assert "q.subject = :subject" in s
        assert "cast(q.difficulty as text) = :level" in s
        assert "q.approved = :approved" in s
    assert "count(*)" in count_sql
    assert "limit :limit offset :offset" in sql
    assert "order by q.subject, q.source_number, q.id" in sql
    assert "answer" in sql  # HR-only view
    assert params == {
        "source_id": SID,
        "subject": "Operating Systems",
        "level": "medium",
        "approved": False,
        "limit": 20,
        "offset": 40,
    }
    assert count_params == {k: params[k] for k in ("source_id", "subject", "level", "approved")}


def test_list_questions_without_filters():
    session = RecordingSession([{"n": 0}], [])
    rows, total = run(repo.list_questions(session, SID))
    assert (rows, total) == ([], 0)
    sql = session.calls[1][0]
    assert ":subject" not in sql and ":level" not in sql and ":approved" not in sql


def test_list_questions_rejects_bad_level():
    with pytest.raises(ValueError):
        run(repo.list_questions(RecordingSession(), SID, level="hard"))


def test_delete_source_cascades_only_via_fk():
    session = RecordingSession([{"storage_key": "k"}])
    row = run(repo.delete_source(session, SID))
    assert row == {"storage_key": "k"}
    [(sql, params)] = session.calls
    assert sql.startswith("delete from question_sources where id = :id")
    assert "questions" not in sql.replace("question_sources", "")  # FK cascades the rest
    assert params == {"id": SID}


def test_delete_questions_only_that_source():
    session = RecordingSession([{"id": 1}, {"id": 2}])
    assert run(repo.delete_questions(session, SID)) == 2
    [(sql, params)] = session.calls
    assert sql.startswith("delete from questions where source_id = :source_id")
    assert params == {"source_id": SID}


def test_search_uses_vector_operator_only_with_query_vec():
    session = RecordingSession()
    run(repo.search_questions(session, track="it", levels=["easy"], per_cell=5))
    sql, params = session.calls[0]
    assert "<=>" not in sql and "question_embeddings" not in sql
    assert "query_vec" not in params

    session = RecordingSession()
    run(
        repo.search_questions(
            session, track="it", levels=["easy"], per_cell=5, query_vec=[0.5, -0.25, 1.0]
        )
    )
    sql, params = session.calls[0]
    assert "e.embedding <=> cast(:query_vec as vector)" in sql
    assert "left join question_embeddings e on e.question_id = q.id" in sql
    assert "nulls last" in sql
    assert params["query_vec"] == "[0.5,-0.25,1.0]"


def test_search_filters_active_approved_track():
    session = RecordingSession()
    run(
        repo.search_questions(
            session,
            track="non_it",
            levels=["medium", "advanced"],
            subjects=["Operating Systems"],
            per_cell=3,
        )
    )
    sql, params = session.calls[0]
    assert "join question_sources s on s.id = q.source_id" in sql
    assert "s.track = :track" in sql
    assert "s.is_active" in sql and "s.status = 'ready'" in sql
    assert "q.approved" in sql
    assert "cast(q.difficulty as text) = any(cast(:levels as text[]))" in sql
    assert "q.subject = any(cast(:subjects as text[]))" in sql
    assert "partition by q.subject, q.difficulty" in sql
    assert "rn <= :per_cell" in sql
    assert params["track"] == "non_it"
    assert params["levels"] == ["medium", "advanced"]
    assert params["subjects"] == ["Operating Systems"]
    assert params["per_cell"] == 3


def test_search_without_subjects_has_no_subject_filter():
    session = RecordingSession()
    run(repo.search_questions(session, track="it", levels=["easy"], per_cell=1))
    sql, params = session.calls[0]
    assert ":subjects" not in sql and "subjects" not in params


def test_counts_nests_subject_level():
    session = RecordingSession(
        [
            {"subject": "Operating Systems", "level": "easy", "total": 3, "approved": 1},
            {"subject": "Operating Systems", "level": "advanced", "total": 2, "approved": 0},
            {"subject": "Computer Networks", "level": "medium", "total": 5, "approved": 5},
        ]
    )
    out = run(repo.counts(session, SID))
    assert out == {
        "Operating Systems": {
            "easy": {"total": 3, "approved": 1},
            "advanced": {"total": 2, "approved": 0},
        },
        "Computer Networks": {"medium": {"total": 5, "approved": 5}},
    }
    sql, params = session.calls[0]
    assert "group by q.subject, q.difficulty" in sql and params == {"source_id": SID}


def test_set_approval_scopes():
    ids = [uuid.uuid4(), uuid.uuid4()]
    session = RecordingSession([{"id": ids[0]}])
    n = run(repo.set_approval(session, SID, ids=ids, approved=True, only_clean=True))
    assert n == 1
    sql, params = session.calls[0]
    assert "update questions set approved = :approved" in sql
    assert "source_id = :source_id" in sql
    assert "id = any(cast(:ids as uuid[]))" in sql
    assert "cardinality(warnings) = 0" in sql
    assert ":subject" not in sql
    assert params == {"source_id": SID, "approved": True, "ids": ids}

    session = RecordingSession()
    run(repo.set_approval(session, SID, subject="Operating Systems", approved=False))
    sql, params = session.calls[0]
    assert "subject = :subject" in sql and ":ids" not in sql and "cardinality" not in sql


def test_update_source_whitelists_and_casts_report():
    session = RecordingSession([{"id": SID}])
    run(repo.update_source(session, SID, {"name": "Core CS", "report": {"total": 1}}))
    sql, params = session.calls[0]
    assert "name = :name" in sql and "report = cast(:report as jsonb)" in sql
    assert "updated_at = now()" in sql
    assert json.loads(params["report"]) == {"total": 1}
    with pytest.raises(ValueError):
        run(repo.update_source(RecordingSession(), SID, {"uploaded_by": uuid.uuid4()}))


def test_update_question_maps_fields():
    qid = uuid.uuid4()
    session = RecordingSession([{"id": qid}])
    run(
        repo.update_question(
            session,
            qid,
            {"text": "New?", "options": ["a", "b", "c", "d"], "answer": 2, "level": "easy"},
        )
    )
    sql, params = session.calls[0]
    assert "body = :text" in sql
    assert "options = cast(:options as jsonb)" in sql
    assert "answer = cast(:answer as jsonb)" in sql
    assert "difficulty = cast(:level as difficulty)" in sql
    assert json.loads(params["options"]) == ["a", "b", "c", "d"]
    assert json.loads(params["answer"]) == 2
    with pytest.raises(ValueError):
        run(repo.update_question(RecordingSession(), qid, {"source_id": SID}))


def test_upsert_embeddings_casts_vector():
    qid = uuid.uuid4()
    session = RecordingSession([{"question_id": qid}])
    n = run(
        repo.upsert_embeddings(
            session, [{"question_id": qid, "embedding": [0.1, 0.2], "model": "m"}]
        )
    )
    assert n == 1
    sql, params = session.calls[0]
    assert "cast(r.embedding as vector)" in sql
    assert "on conflict (question_id) do update" in sql
    assert json.loads(params["rows"]) == [
        {"question_id": str(qid), "embedding": "[0.1,0.2]", "model": "m"}
    ]


def test_set_embedding_status_validates():
    session = RecordingSession()
    run(repo.set_embedding_status(session, SID, "done"))
    sql, params = session.calls[0]
    assert "embedding_status = :status" in sql and params == {"id": SID, "status": "done"}
    with pytest.raises(ValueError):
        run(repo.set_embedding_status(RecordingSession(), SID, "weird"))


def test_get_source_for_update_locks():
    session = RecordingSession([{"id": SID}])
    run(repo.get_source(session, SID, for_update=True))
    assert session.calls[0][0].endswith("for update")

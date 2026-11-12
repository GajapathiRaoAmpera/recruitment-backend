"""Gemini embedder + embed_source / embed_question. httpx.MockTransport and fakes only: no network,
no DB."""

import asyncio
import json
import logging
import uuid

import httpx
import pytest

from app.core.errors import AppError
from app.qbank import embeddings
from app.qbank.embeddings import (
    EmbeddingOutputError,
    GeminiEmbedder,
    document_text,
    embed_question,
    embed_source,
    make_embedder,
)

SECRET = "AIza-super-secret-key-123"
DIM = 4


def run(coro):
    return asyncio.run(coro)


async def no_sleep(_s: float) -> None:
    return None


def vec(i: int) -> list[float]:
    return [float(i + 1), 0.0, 0.0, 0.0]


def ok_handler(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request, body))
        n = len(body["requests"])
        start = sum(len(b["requests"]) for _, b in calls[:-1])
        return httpx.Response(
            200, json={"embeddings": [{"values": vec(start + i)} for i in range(n)]}
        )

    return handler


def embedder(handler, model="gemini-embedding-2", dim=DIM) -> GeminiEmbedder:
    return GeminiEmbedder(
        SECRET, model, dim=dim, sleep=no_sleep, transport=httpx.MockTransport(handler)
    )


# ------------------------------------------------------------------ GeminiEmbedder


def test_batches_of_100():
    calls: list = []
    e = embedder(ok_handler(calls))
    out = run(e.embed([f"q{i}" for i in range(250)], task="document"))
    assert [len(b["requests"]) for _, b in calls] == [100, 100, 50]
    assert len(out) == 250
    # order preserved across batches (vectors are L2-normalised: the first value is 1.0)
    assert all(v == [1.0, 0.0, 0.0, 0.0] for v in out)
    req, body = calls[0]
    assert str(req.url).endswith("/v1beta/models/gemini-embedding-2:batchEmbedContents")
    assert req.headers["x-goog-api-key"] == SECRET
    assert SECRET not in str(req.url)
    item = body["requests"][0]
    assert item["model"] == "models/gemini-embedding-2"
    assert item["embedContentConfig"]["outputDimensionality"] == DIM
    # gemini-embedding-2 takes the task as a text prefix, not taskType
    assert "taskType" not in item["embedContentConfig"]
    assert item["content"]["parts"][0]["text"] == "title: none | text: q0"


def test_query_task_prefix_for_embedding_2():
    calls: list = []
    run(embedder(ok_handler(calls)).embed(["graphs"], task="query"))
    item = calls[0][1]["requests"][0]
    assert item["content"]["parts"][0]["text"] == "task: search result | query: graphs"


def test_task_type_field_for_embedding_001():
    calls: list = []
    e = embedder(ok_handler(calls), model="gemini-embedding-001")
    run(e.embed(["doc"], task="document"))
    run(e.embed(["q"], task="query"))
    doc, query = calls[0][1]["requests"][0], calls[1][1]["requests"][0]
    assert doc["embedContentConfig"] == {
        "taskType": "RETRIEVAL_DOCUMENT",
        "outputDimensionality": 4,
    }
    assert query["embedContentConfig"]["taskType"] == "RETRIEVAL_QUERY"
    assert doc["content"]["parts"][0]["text"] == "doc"


def test_empty_input_makes_no_request():
    calls: list = []
    assert run(embedder(ok_handler(calls)).embed([], task="document")) == []
    assert calls == []


def test_vectors_are_normalised():
    def handler(request):
        return httpx.Response(200, json={"embeddings": [{"values": [3.0, 4.0, 0.0, 0.0]}]})

    (v,) = run(embedder(handler).embed(["x"], task="document"))
    assert v == pytest.approx([0.6, 0.8, 0.0, 0.0])


@pytest.mark.parametrize(
    "payload",
    [
        {"embeddings": [{"values": [1.0, 2.0, 3.0]}]},  # wrong length
        {"embeddings": [{"values": [1.0, float("nan"), 0.0, 0.0]}]},
        {"embeddings": [{"values": [1.0, float("inf"), 0.0, 0.0]}]},
        {"embeddings": [{"values": [1.0, "0.5", 0.0, 0.0]}]},  # not a number
        {"embeddings": [{"values": [0.0, 0.0, 0.0, 0.0]}]},  # zero vector: no direction
        {"embeddings": [{"values": [1.0, True, 0.0, 0.0]}]},
        {"embeddings": []},  # count mismatch
        {"embeddings": [{"values": [1, 0, 0, 0]}, {"values": [1, 0, 0, 0]}]},
        {"nothing": 1},
        {"embeddings": [{"values": None}]},
    ],
)
def test_bad_vectors_rejected_safely(payload):
    def handler(request):
        # json.dumps writes NaN / Infinity literals, as a misbehaving server might
        content = json.dumps(payload)
        return httpx.Response(200, content=content, headers={"content-type": "application/json"})

    with pytest.raises(EmbeddingOutputError) as exc:
        run(embedder(handler).embed(["x"], task="document"))
    assert SECRET not in str(exc.value)


def test_non_json_reply_rejected():
    def handler(request):
        return httpx.Response(200, content=b"<html>oops</html>")

    with pytest.raises(EmbeddingOutputError):
        run(embedder(handler).embed(["x"], task="document"))


def test_key_never_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)

    def server_error(request):
        return httpx.Response(500, text=f"echo {SECRET}")

    def network(request):
        raise httpx.ConnectError(f"failed for {request.url} key={SECRET}")

    def client_error(request):
        return httpx.Response(400, text=f"bad key {SECRET}")

    for handler in (server_error, network, client_error):
        e = embedder(handler)
        with pytest.raises(AppError) as exc:
            run(e.embed(["x"], task="document"))
        assert exc.value.code == "AI_PROVIDER_ERROR"
        assert SECRET not in str(exc.value) and SECRET not in repr(exc.value.details)
        assert SECRET not in repr(e)
    assert SECRET not in caplog.text


def test_rate_limit_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"details": []}})
        return httpx.Response(200, json={"embeddings": [{"values": [1, 0, 0, 0]}]})

    assert len(run(embedder(handler).embed(["x"], task="query"))) == 1
    assert calls["n"] == 2


def test_missing_key_not_configured(monkeypatch):
    with pytest.raises(AppError) as exc:
        GeminiEmbedder("", "gemini-embedding-2")
    assert exc.value.code == "AI_PROVIDER_NOT_CONFIGURED"

    class S:
        gemini_api_key = None
        gemini_embedding_model = "gemini-embedding-2"
        embedding_dim = 768

    monkeypatch.setattr(embeddings, "get_settings", lambda: S())
    assert make_embedder() is None
    S.gemini_api_key = SECRET
    e = make_embedder()
    assert isinstance(e, GeminiEmbedder)
    assert e.model == "gemini-embedding-2" and e.dim == 768


def test_default_model_and_dim_from_settings():
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["gemini_embedding_model"].default == "gemini-embedding-2"
    assert fields["embedding_dim"].default == 768


def test_document_text_is_what_the_candidate_sees():
    t = document_text("What is a deadlock?", ["A wait cycle", "A crash", "A loop", "A leak"])
    assert t == "What is a deadlock?\nA. A wait cycle\nB. A crash\nC. A loop\nD. A leak"


def test_models_prefix_stripped_from_model_name():
    calls: list = []
    e = embedder(ok_handler(calls), model=" models/gemini-embedding-2 ")
    assert e.model == "gemini-embedding-2"
    run(e.embed(["x"], task="query"))
    req, body = calls[0]
    assert str(req.url).endswith("/models/gemini-embedding-2:batchEmbedContents")
    assert body["requests"][0]["model"] == "models/gemini-embedding-2"


# ------------------------------------------------------------------ embed_source

SID = uuid.uuid4()


def qrow(i: int) -> dict:
    return {
        "id": uuid.UUID(int=i + 1),
        "text": f"Question {i}?",
        "options": ["a", "b", "c", "d"],
        "answer": 2,
        "explanation": "SECRET-EXPLANATION",
    }


class FakeDb:
    """Committed state + sessions that stage writes until commit (discarded on close)."""

    def __init__(self, n_questions: int = 5):
        self.source: dict | None = {"id": SID, "sha256": "sha-1", "embedding_status": "pending"}
        self.questions = {q["id"]: q for q in (qrow(i) for i in range(n_questions))}
        self.embeddings: dict = {}
        self.open = 0  # sessions currently open
        self.fail_open = False

    def session(self) -> "FakeSession":
        if self.fail_open:
            raise OSError("cannot connect")
        return FakeSession(self)


class FakeSession:
    def __init__(self, db: FakeDb | None = None):
        self.db = db
        self.commits = 0
        self.staged: list = []
        self.executed: list[tuple[str, dict]] = []

    async def __aenter__(self):
        if self.db:
            self.db.open += 1
        return self

    async def __aexit__(self, *exc):
        if self.db:
            self.db.open -= 1
        self.staged = []  # closing without commit = rollback
        return False

    async def commit(self):
        self.commits += 1
        for apply in self.staged:
            apply()
        self.staged = []

    async def rollback(self):
        self.staged = []

    async def execute(self, stmt, params=None):
        self.executed.append((" ".join(str(stmt).split()), params or {}))


class FakeEmbedder:
    model = "fake-embed"

    def __init__(self, fail: Exception | None = None, during=None, db: FakeDb | None = None):
        self.calls: list[tuple[list[str], str]] = []
        self.open_during_embed: list[int] = []
        self.fail = fail
        self.during = during
        self.db = db

    async def embed(self, texts, *, task):
        self.calls.append((list(texts), task))
        if self.db is not None:
            self.open_during_embed.append(self.db.open)
        if self.during:
            self.during()
        if self.fail:
            raise self.fail
        return [[1.0, float(i), 0.0, 0.0] for i in range(len(texts))]


@pytest.fixture
def db(monkeypatch):
    d = FakeDb()

    async def get_source(session, source_id, *, for_update=False):
        return dict(d.source) if d.source and source_id == d.source["id"] else None

    async def question_texts(session, source_id, *, for_update=False):
        return [dict(q) for _, q in sorted(d.questions.items())]

    async def upsert_embeddings(session, rows):
        rows = list(rows)
        session.staged.append(lambda: d.embeddings.update({r["question_id"]: r for r in rows}))
        return len(rows)

    async def set_embedding_status(session, source_id, status):
        session.staged.append(lambda: d.source.update(embedding_status=status))

    async def set_embedding_status_if(session, source_id, status, *, sha256):
        if d.source is None or d.source["sha256"] != sha256:
            return False
        session.staged.append(lambda: d.source.update(embedding_status=status))
        return True

    monkeypatch.setattr(embeddings, "open_session", d.session)
    monkeypatch.setattr(embeddings.repo, "get_source", get_source)
    monkeypatch.setattr(embeddings.repo, "question_texts", question_texts)
    monkeypatch.setattr(embeddings.repo, "upsert_embeddings", upsert_embeddings)
    monkeypatch.setattr(embeddings.repo, "set_embedding_status", set_embedding_status)
    monkeypatch.setattr(embeddings.repo, "set_embedding_status_if", set_embedding_status_if)
    return d


def test_embed_source_marks_done(db):
    fake = FakeEmbedder(db=db)
    assert run(embed_source(SID, fake)) == "done"
    assert db.source["embedding_status"] == "done"
    ((texts, task),) = fake.calls
    assert task == "document" and len(texts) == 5
    assert all("SECRET-EXPLANATION" not in t for t in texts)
    assert texts[0] == "Question 0?\nA. a\nB. b\nC. c\nD. d"
    assert set(db.embeddings) == set(db.questions)
    assert all(r["model"] == "fake-embed" for r in db.embeddings.values())


def test_no_session_open_while_embedding(db):
    fake = FakeEmbedder(db=db)
    run(embed_source(SID, fake))
    assert fake.open_during_embed == [0]
    assert db.open == 0


def test_edit_during_embed_does_not_store_stale_vector(db):
    edited, gone = qrow(2)["id"], qrow(3)["id"]

    def edit_and_delete():
        db.questions[edited] = {**db.questions[edited], "text": "Edited text?"}
        del db.questions[gone]

    assert run(embed_source(SID, FakeEmbedder(during=edit_and_delete, db=db))) == "done"
    assert edited not in db.embeddings  # embed_question owns the new text's vector
    assert gone not in db.embeddings
    assert set(db.embeddings) == set(db.questions) - {edited}
    assert db.source["embedding_status"] == "done"


def replace_source(db: FakeDb):
    def replace():
        db.source.update(sha256="sha-2", embedding_status="pending")
        db.questions = {q["id"]: q for q in (qrow(i) for i in range(100, 103))}

    return replace


def test_replace_during_embed_leaves_new_upload_alone(db):
    assert run(embed_source(SID, FakeEmbedder(during=replace_source(db), db=db))) is None
    assert db.source["embedding_status"] == "pending"
    assert db.embeddings == {}


def test_replace_during_failed_embed_does_not_write_failed_over_pending(db):
    fake = FakeEmbedder(fail=RuntimeError("down"), during=replace_source(db), db=db)
    assert run(embed_source(SID, fake)) == "failed"
    assert db.source["embedding_status"] == "pending"


def test_batch_3_of_5_failure_stores_nothing_and_marks_failed(db):
    db.questions = {q["id"]: q for q in (qrow(i) for i in range(450))}
    calls: list = []

    def handler(request):
        calls.append(1)
        if len(calls) == 3:
            return httpx.Response(400, json={"error": {"message": "bad"}})
        n = len(json.loads(request.content)["requests"])
        return httpx.Response(200, json={"embeddings": [{"values": [1, 0, 0, 0]}] * n})

    assert run(embed_source(SID, embedder(handler))) == "failed"
    assert len(calls) == 3
    assert db.embeddings == {}
    assert db.source["embedding_status"] == "failed"


def test_embed_source_failure_marks_failed_not_raised(db, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeEmbedder(fail=RuntimeError(f"provider said {SECRET}"), db=db)
    assert run(embed_source(SID, fake)) == "failed"
    assert db.source["embedding_status"] == "failed"
    assert db.embeddings == {}
    assert "RuntimeError" in caplog.text
    assert SECRET not in caplog.text


def test_embed_source_status_write_failure_not_raised(db, monkeypatch):
    async def broken(session, source_id, status, **kw):
        raise OSError("db down")

    monkeypatch.setattr(embeddings.repo, "set_embedding_status", broken)
    monkeypatch.setattr(embeddings.repo, "set_embedding_status_if", broken)
    assert run(embed_source(SID, FakeEmbedder(db=db))) == "failed"


def test_embed_source_never_raises_when_db_unreachable(db):
    db.fail_open = True
    assert run(embed_source(SID, FakeEmbedder(db=db))) == "failed"
    assert run(embed_source(SID, None)) == "failed"


def test_no_key_marks_skipped(db):
    assert run(embed_source(SID, None)) == "skipped"
    assert db.source["embedding_status"] == "skipped"
    assert db.embeddings == {}


def test_embed_source_missing_source_is_noop(db):
    assert run(embed_source(uuid.uuid4(), FakeEmbedder(db=db))) is None
    assert db.source["embedding_status"] == "pending"


def test_embed_source_empty_bank_is_done(db):
    db.questions = {}
    fake = FakeEmbedder(db=db)
    assert run(embed_source(SID, fake)) == "done"
    assert fake.calls == []
    assert db.source["embedding_status"] == "done"


class RecordingSession:
    def __init__(self, rows=()):
        self.executed: list[tuple[str, dict]] = []
        self._rows = list(rows)

    async def execute(self, stmt, params=None):
        self.executed.append((" ".join(str(stmt).split()), params or {}))
        rows = self._rows

        class R:
            def mappings(self):
                return self

            def all(self):
                return rows

        return R()


def test_job_repo_sql():
    from app.qbank import repo

    s = RecordingSession(rows=[{"id": SID}])
    assert run(repo.set_embedding_status_if(s, SID, "done", sha256="abc")) is True
    sql, params = s.executed[-1]
    assert "where id = :id and sha256 = :sha256" in sql
    assert params == {"id": SID, "status": "done", "sha256": "abc"}
    with pytest.raises(ValueError):
        run(repo.set_embedding_status_if(s, SID, "weird", sha256="abc"))
    run(repo.question_texts(s, SID, for_update=True))
    sql, params = s.executed[-1]
    assert sql.startswith("select q.id, q.body as text, q.options from questions q")
    assert sql.endswith("for update")
    assert params == {"source_id": SID}


# ------------------------------------------------------------------ embed_question


@pytest.fixture
def one_question(monkeypatch):
    state = {"row": qrow(7), "upserts": []}

    async def get_question(session, question_id, *, for_update=False):
        return state["row"] if state["row"] and question_id == state["row"]["id"] else None

    async def upsert_embeddings(session, rows):
        state["upserts"].extend(rows)
        return len(rows)

    monkeypatch.setattr(embeddings.repo, "get_question", get_question)
    monkeypatch.setattr(embeddings.repo, "upsert_embeddings", upsert_embeddings)
    return state


def test_embed_question_reembeds_edited_question(one_question):
    session, fake = FakeSession(), FakeEmbedder()
    qid = one_question["row"]["id"]
    assert run(embed_question(session, qid, fake)) is True
    assert fake.calls == [(["Question 7?\nA. a\nB. b\nC. c\nD. d"], "document")]
    assert one_question["upserts"][0]["question_id"] == qid
    assert session.commits == 0  # caller owns the transaction


@pytest.mark.parametrize("fake", [None, FakeEmbedder(fail=RuntimeError("boom"))])
def test_embed_question_drops_stale_vector_when_it_cannot_embed(one_question, fake):
    session = FakeSession()
    qid = one_question["row"]["id"]
    assert run(embed_question(session, qid, fake)) is False
    assert one_question["upserts"] == []
    ((sql, params),) = session.executed  # the real repo.delete_embedding
    assert sql == "delete from question_embeddings where question_id = :id"
    assert params == {"id": qid}


def test_embed_question_missing_question(one_question):
    assert run(embed_question(FakeSession(), uuid.uuid4(), FakeEmbedder())) is False

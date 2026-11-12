"""Local embedder (fastembed, faked here: no model download) + embed_source / embed_question.
Fakes only: no network, no DB."""

import asyncio
import logging
import uuid

import pytest

from app.qbank import embeddings
from app.qbank.embeddings import (
    EmbeddingOutputError,
    LocalEmbedder,
    document_text,
    embed_question,
    embed_source,
    make_embedder,
)

SECRET = "super-secret-123"
DIM = 4


def run(coro):
    return asyncio.run(coro)


class FakeModel:
    """Stands in for fastembed.TextEmbedding: records calls, returns one vector per text."""

    def __init__(self, vectors=None, fail_after: int | None = None):
        self.vectors = vectors
        self.fail_after = fail_after
        self.calls: list[tuple[list[str], int]] = []

    def embed(self, texts, batch_size=64):
        self.calls.append((list(texts), batch_size))
        for i, _ in enumerate(texts):
            if self.fail_after is not None and i >= self.fail_after:
                raise RuntimeError("onnx run failed")
            yield list(self.vectors[i]) if self.vectors else [float(i + 1), 0.0, 0.0, 0.0]


def local(model: FakeModel | None = None, dim: int = DIM) -> tuple[LocalEmbedder, FakeModel]:
    """A LocalEmbedder whose shared model slot holds a fake (never loads fastembed)."""
    fake = model or FakeModel()
    e = LocalEmbedder("fake/model", dim=dim, cache_dir=f"/tmp/{uuid.uuid4()}")
    LocalEmbedder._models[(e.model, e.cache_dir)] = fake
    return e, fake


# ------------------------------------------------------------------ LocalEmbedder


def test_embeds_in_a_thread_with_batching_and_normalises():
    e, fake = local(FakeModel(vectors=[[3.0, 4.0, 0.0, 0.0], [0.0, 2.0, 0.0, 0.0]]))
    out = run(e.embed(["a", "b"], task="document"))
    assert out == [pytest.approx([0.6, 0.8, 0.0, 0.0]), pytest.approx([0.0, 1.0, 0.0, 0.0])]
    assert fake.calls == [(["a", "b"], embeddings.BATCH_SIZE)]


def test_query_and_document_texts_are_embedded_as_is():
    e, fake = local()
    run(e.embed(["graphs"], task="query"))
    assert fake.calls[0][0] == ["graphs"]  # MiniLM: no task prefixes


def test_empty_input_runs_nothing():
    e, fake = local()
    assert run(e.embed([], task="document")) == []
    assert fake.calls == []


@pytest.mark.parametrize(
    "vector",
    [
        [1.0, 2.0, 3.0],  # wrong length
        [1.0, float("nan"), 0.0, 0.0],
        [1.0, float("inf"), 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0],  # zero vector: no direction
    ],
)
def test_bad_vectors_rejected(vector):
    e, _ = local(FakeModel(vectors=[vector]))
    with pytest.raises(EmbeddingOutputError):
        run(e.embed(["x"], task="document"))


def test_model_is_loaded_once_and_shared(monkeypatch):
    loads: list[tuple] = []

    class FakeTextEmbedding(FakeModel):
        def __init__(self, model_name, cache_dir):
            super().__init__()
            loads.append((model_name, cache_dir))

    import fastembed

    monkeypatch.setattr(fastembed, "TextEmbedding", FakeTextEmbedding)
    cache = f"/tmp/{uuid.uuid4()}"
    a = LocalEmbedder("sentence-transformers/all-MiniLM-L6-v2", dim=DIM, cache_dir=cache)
    b = LocalEmbedder("sentence-transformers/all-MiniLM-L6-v2", dim=DIM, cache_dir=cache)
    run(a.embed(["x"], task="document"))
    run(b.embed(["y"], task="query"))
    assert loads == [("sentence-transformers/all-MiniLM-L6-v2", cache)]


def test_make_embedder_uses_settings_and_needs_no_key():
    e = make_embedder()
    assert isinstance(e, LocalEmbedder)
    assert e.model == "sentence-transformers/all-MiniLM-L6-v2" and e.dim == 384


def test_default_model_and_dim_match_migration():
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["embedding_model"].default == "sentence-transformers/all-MiniLM-L6-v2"
    assert fields["embedding_dim"].default == 384
    from pathlib import Path

    migration = Path(__file__).parents[2] / "migrations/versions/0004_question_bank.py"
    assert "vector(384)" in migration.read_text()


def test_document_text_is_what_the_candidate_sees():
    t = document_text("What is a deadlock?", ["A wait cycle", "A crash", "A loop", "A leak"])
    assert t == "What is a deadlock?\nA. A wait cycle\nB. A crash\nC. A loop\nD. A leak"


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


def test_failure_midway_stores_nothing_and_marks_failed(db):
    db.questions = {q["id"]: q for q in (qrow(i) for i in range(450))}
    e, fake = local(FakeModel(fail_after=200))
    assert run(embed_source(SID, e)) == "failed"
    assert len(fake.calls) == 1
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

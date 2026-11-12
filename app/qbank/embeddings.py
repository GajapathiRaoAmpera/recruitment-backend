"""Question-bank embeddings (the "R" of RAG): all-MiniLM-L6-v2 run locally with fastembed (ONNX).

Free, no API key, no quota: the model (~90 MB) is downloaded once into settings.embedding_cache_dir
on first use and runs on the server CPU, in a worker thread so the event loop is never blocked.

What is embedded is what a candidate sees - the question text and its options - never the answer
or the explanation. Vectors are validated (exactly settings.embedding_dim = 384 finite numbers,
question_embeddings is vector(384)) and L2-normalised before storage. MiniLM uses no query /
document prefixes, so `task` does not change the text.
"""

import asyncio
import json
import logging
import math
import threading
from collections.abc import Sequence
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.db import get_engine
from app.papers.agents import LlmOutputError
from app.qbank import repo

log = logging.getLogger(__name__)

Task = Literal["document", "query"]

BATCH_SIZE = 64  # texts per ONNX run

_LETTERS = "ABCDEFGH"


class Embedder(Protocol):
    model: str  # stored with each vector; retrieval only compares vectors of the same model

    async def embed(self, texts: Sequence[str], *, task: Task) -> list[list[float]]: ...


class EmbeddingOutputError(LlmOutputError):
    """The provider answered, but not with usable vectors (message never echoes the reply)."""


def document_text(text: str, options: Sequence[str]) -> str:
    """The text embedded for a bank question: stem + lettered options, nothing else."""
    lines = [text.strip()]
    lines.extend(f"{_LETTERS[i]}. {str(o).strip()}" for i, o in enumerate(options))
    return "\n".join(lines)


def check_vector(values: Any, dim: int) -> list[float]:
    """Exactly `dim` finite numbers with a non-zero norm -> the L2-normalised vector.
    Anything else raises EmbeddingOutputError (pgvector rejects NaN / inf / wrong length)."""
    if not isinstance(values, list) or len(values) != dim:
        raise EmbeddingOutputError(f"embedding does not have {dim} values")
    if any(isinstance(v, bool) or not isinstance(v, int | float) for v in values):
        raise EmbeddingOutputError("embedding has non-numeric values")
    floats = [float(v) for v in values]
    if not all(math.isfinite(v) for v in floats):
        raise EmbeddingOutputError("embedding has non-finite values")
    norm = math.sqrt(math.fsum(v * v for v in floats))
    if not norm or not math.isfinite(norm):
        raise EmbeddingOutputError("embedding has no direction (zero vector)")
    return [v / norm for v in floats]


class LocalEmbedder:
    """fastembed TextEmbedding, loaded lazily once per process and shared (ONNX sessions are
    heavy; a lock serialises runs so concurrent jobs never load or run it twice at once)."""

    _models: dict[tuple[str, str], Any] = {}
    _lock = threading.Lock()

    def __init__(
        self, model: str | None = None, *, dim: int | None = None, cache_dir: str | None = None
    ):
        s = get_settings()
        self.model = model or s.embedding_model
        self.dim = dim or s.embedding_dim
        self.cache_dir = cache_dir or s.embedding_cache_dir

    def __repr__(self) -> str:
        return f"LocalEmbedder(model={self.model!r})"

    def _run(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            key = (self.model, self.cache_dir)
            if key not in self._models:
                from fastembed import TextEmbedding  # import on first use: keeps startup fast

                self._models[key] = TextEmbedding(model_name=self.model, cache_dir=self.cache_dir)
            vectors = list(self._models[key].embed(texts, batch_size=BATCH_SIZE))
        return [check_vector([float(v) for v in vec], self.dim) for vec in vectors]

    async def embed(self, texts: Sequence[str], *, task: Task) -> list[list[float]]:
        if not texts:
            return []
        out = await asyncio.to_thread(self._run, list(texts))
        if len(out) != len(texts):
            raise EmbeddingOutputError("embedder returned the wrong number of vectors")
        return out


def make_embedder() -> Embedder:
    """The configured embedder (local, always available; a failed model download marks the
    bank 'failed' and retrieval falls back to its deterministic order)."""
    return LocalEmbedder()


def open_session() -> AsyncSession:
    """A session of its own for the background job (same pattern as papers.service)."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)()


def _options(value: Any) -> list[str]:
    if isinstance(value, str | bytes):  # jsonb may come back undecoded
        value = json.loads(value)
    return [str(o) for o in value or []]


def _row_text(row: Any) -> str:
    return document_text(row["text"], _options(row["options"]))


async def _vectors(texts: Sequence[str], embedder: Embedder) -> list[list[float]]:
    vectors = await embedder.embed(list(texts), task="document")
    if len(vectors) != len(texts):
        raise EmbeddingOutputError("embedder returned the wrong number of vectors")
    return vectors


async def _finish(source_id: UUID, sha256: str | None, status: str) -> None:
    """Record a status in a short transaction of its own, only while the source still holds the
    file the job started from (a replace changes sha256 and resets the status itself)."""
    if sha256 is None:  # never got as far as reading the source: nothing safe to write
        return
    try:
        async with open_session() as session:
            await repo.set_embedding_status_if(session, source_id, status, sha256=sha256)
            await session.commit()
    except Exception as e:
        log.warning("could not record embedding status (%s)", type(e).__name__)


async def embed_source(source_id: UUID, embedder: Embedder | None) -> str | None:
    """Background job: embed every question of a bank and set its embedding_status.

    Three phases, so no DB transaction is open during the provider calls:
      1. short read: the source (its sha256 = which upload this job is for) + {id, text, options};
      2. embed outside any session;
      3. short write: lock the source and its questions, give up if the source is gone or was
         replaced (sha256 changed), store vectors only for questions that still exist with the
         exact text that was embedded (edited ones are re-embedded by embed_question), set 'done'.
    Returns 'skipped' (embedder None, no key), 'done', 'failed' (any error; status written only
    if the source was not replaced meanwhile), or None when the source is gone / was replaced.
    Never raises; logs only exception type names (provider messages can echo input).
    """
    sha256: str | None = None
    try:
        async with open_session() as session:
            source = await repo.get_source(session, source_id)
            if source is None:
                return None
            sha256 = source["sha256"]
            rows = [] if embedder is None else await repo.question_texts(session, source_id)
        if embedder is None:
            await _finish(source_id, sha256, "skipped")
            return "skipped"

        texts = {r["id"]: _row_text(r) for r in rows}
        vectors = await _vectors(list(texts.values()), embedder) if texts else []

        async with open_session() as session:
            source = await repo.get_source(session, source_id, for_update=True)
            if source is None or source["sha256"] != sha256:
                return None  # deleted or replaced while embedding: the newer job owns it
            current = {
                r["id"]: _row_text(r)
                for r in await repo.question_texts(session, source_id, for_update=True)
            }
            fresh = [
                {"question_id": qid, "embedding": vec, "model": embedder.model}
                for (qid, embedded), vec in zip(texts.items(), vectors, strict=True)
                if current.get(qid) == embedded
            ]
            await repo.upsert_embeddings(session, fresh)
            await repo.set_embedding_status(session, source_id, "done")
            await session.commit()
            return "done"
    except Exception as e:  # background job: record the failure, never raise
        log.warning("embedding bank failed (%s)", type(e).__name__)
        await _finish(source_id, sha256, "failed")
        return "failed"


async def embed_question(
    session: AsyncSession, question_id: UUID, embedder: Embedder | None
) -> bool:
    """Re-embed one question after an edit, in the caller's transaction (no commit).

    True when a fresh vector was stored. When it cannot be embedded (no embedder, provider
    error, bad vectors) the now-stale vector is deleted instead and False is returned, so
    retrieval never ranks by an old text. Missing question -> False. DB errors propagate.
    """
    row = await repo.get_question(session, question_id)
    if row is None:
        return False
    if embedder is not None:
        try:
            (vector,) = await _vectors([_row_text(row)], embedder)
        except Exception as e:  # provider / output problem: the edit itself must still succeed
            log.warning("re-embedding a question failed (%s)", type(e).__name__)
        else:
            await repo.upsert_embeddings(
                session, [{"question_id": row["id"], "embedding": vector, "model": embedder.model}]
            )
            return True
    await repo.delete_embedding(session, question_id)
    return False


# ---------------------------------------------------------------- after-edit job (Task 4)


async def run_reembed_question(question_id: UUID) -> bool | None:
    """Background job after a question edit (the edit already deleted the stale vector).

    Same three phases as embed_source, so no transaction is open during the provider call:
    short read of text + options; embed outside any session; short write that locks the
    question and stores the vector only if its current text + options still equal what was
    embedded (a newer edit or a replace wins). Returns True when stored, False when dropped
    or failed, None when the question is gone. Never raises; logs only
    exception type names.
    """
    try:
        embedder = make_embedder()
        async with open_session() as session:
            row = await repo.get_question(session, question_id)
        if row is None:
            return None
        embedded = _row_text(row)
        (vector,) = await _vectors([embedded], embedder)
        async with open_session() as session:
            current = await repo.get_question(session, question_id, for_update=True)
            if current is None or _row_text(current) != embedded:
                return False  # edited again or replaced meanwhile: its own job embeds it
            await repo.upsert_embeddings(
                session,
                [{"question_id": question_id, "embedding": vector, "model": embedder.model}],
            )
            await session.commit()
            return True
    except Exception as e:  # background job: never raise
        log.warning("re-embedding an edited question failed (%s)", type(e).__name__)
        return False

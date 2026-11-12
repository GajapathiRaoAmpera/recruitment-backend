"""All SQL for question banks: sources (uploaded PDFs), their questions, counts, embeddings and
retrieval. Rows come back as RowMapping (dict-like); the caller owns the transaction.

Bank questions live in the shared `questions` table (kind 'mcq', ai_generated false, approved
false on insert) with source_id / subject / topic / source_number / warnings (migration 0003).
Deleting a source removes its questions and embeddings through the FK cascades.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.papers.schemas import LEVELS
from app.qbank.parser import ParsedQuestion

SOURCE_COLS = (
    "s.id, s.name, s.filename, s.storage_key, s.sha256, s.track, s.notes, s.status, "
    "s.is_active, s.uploaded_by, s.report, s.embedding_status, s.created_at, s.updated_at"
)
# HR/Super Admin view only: includes the answer
QUESTION_COLS = (
    "q.id, q.source_id, q.subject, q.topic, cast(q.difficulty as text) as level, "
    "q.body as text, q.options, q.answer, q.explanation, q.approved, q.warnings, "
    "q.source_number, q.created_at"
)

SOURCE_FIELDS = {  # update_source: field -> SQL expression for its bind param
    "name": ":name",
    "filename": ":filename",
    "storage_key": ":storage_key",
    "sha256": ":sha256",
    "track": ":track",
    "notes": ":notes",
    "status": ":status",
    "is_active": ":is_active",
    "report": "cast(:report as jsonb)",
    "embedding_status": ":embedding_status",
}
QUESTION_FIELDS = {  # update_question: field -> (column, SQL expression for its bind param)
    "text": ("body", ":text"),
    "options": ("options", "cast(:options as jsonb)"),
    "answer": ("answer", "cast(:answer as jsonb)"),
    "level": ("difficulty", "cast(:level as difficulty)"),
    "subject": ("subject", ":subject"),
    "topic": ("topic", ":topic"),
    "explanation": ("explanation", ":explanation"),
    "approved": ("approved", ":approved"),
    "warnings": ("warnings", "cast(:warnings as text[])"),
}
JSON_FIELDS = {"report", "options", "answer"}
EMBEDDING_STATUSES = ("pending", "done", "failed", "skipped")


def _j(value: object) -> str:
    return json.dumps(value, default=str)


def _vec(values: Sequence[float]) -> str:
    """pgvector text input: '[0.1,0.2]'."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _check_level(level: str) -> None:
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")


def _set_clause(fields: Mapping[str, object], allowed: Mapping, kind: str) -> tuple[str, dict]:
    unknown = set(fields) - set(allowed)
    if unknown or not fields:
        raise ValueError(f"cannot update {kind} fields {sorted(unknown) or '(none)'}")
    parts, params = [], {}
    for name, value in fields.items():
        spec = allowed[name]
        col, expr = spec if isinstance(spec, tuple) else (name, spec)
        parts.append(f"{col} = {expr}")
        params[name] = _j(value) if name in JSON_FIELDS else value
    return ", ".join(parts), params


# ---------------------------------------------------------------- sources


async def insert_source(
    session: AsyncSession,
    *,
    name: str,
    filename: str,
    storage_key: str,
    sha256: str,
    track: str,
    notes: str | None,
    uploaded_by: UUID,
    report: dict,
    status: str = "ready",
) -> RowMapping:
    """Insert a bank file row. Returns the row: SOURCE_COLS (id, name, filename, storage_key,
    sha256, track, notes, status, is_active, uploaded_by, report, embedding_status, created_at,
    updated_at). embedding_status starts 'pending'."""
    rows = await session.execute(
        text(
            "insert into question_sources as s (name, filename, storage_key, sha256, track, notes, "
            "status, uploaded_by, report) values (:name, :filename, :storage_key, :sha256, "
            ":track, :notes, :status, :uploaded_by, cast(:report as jsonb)) "
            f"returning {SOURCE_COLS}"
        ),
        {
            "name": name,
            "filename": filename,
            "storage_key": storage_key,
            "sha256": sha256,
            "track": track,
            "notes": notes,
            "status": status,
            "uploaded_by": uploaded_by,
            "report": _j(report),
        },
    )
    return rows.mappings().one()


async def get_source(
    session: AsyncSession, source_id: UUID, *, for_update: bool = False
) -> RowMapping | None:
    """One source (SOURCE_COLS) or None. for_update=True row-locks it until the transaction ends
    (replace / delete / approve run one at a time per bank)."""
    lock = " for update" if for_update else ""
    rows = await session.execute(
        text(f"select {SOURCE_COLS} from question_sources s where s.id = :id{lock}"),
        {"id": source_id},
    )
    return rows.mappings().one_or_none()


async def get_source_by_hash(session: AsyncSession, sha256: str) -> RowMapping | None:
    """The source with this file hash (SOURCE_COLS) or None; used for duplicate-upload checks."""
    rows = await session.execute(
        text(f"select {SOURCE_COLS} from question_sources s where s.sha256 = :sha256"),
        {"sha256": sha256},
    )
    return rows.mappings().one_or_none()


async def list_sources(session: AsyncSession) -> list[RowMapping]:
    """Every source, newest first: SOURCE_COLS + question_count, approved_count (ints)."""
    rows = await session.execute(
        text(
            f"select {SOURCE_COLS}, count(q.id) as question_count, "
            "count(q.id) filter (where q.approved) as approved_count "
            "from question_sources s left join questions q on q.source_id = s.id "
            "group by s.id order by s.created_at desc"
        )
    )
    return list(rows.mappings().all())


async def update_source(
    session: AsyncSession, source_id: UUID, fields: Mapping[str, object]
) -> RowMapping | None:
    """Set the given fields (keys of SOURCE_FIELDS; report is JSON-encoded) and updated_at.
    Returns the updated row (SOURCE_COLS) or None when missing. Unknown fields -> ValueError."""
    sets, params = _set_clause(fields, SOURCE_FIELDS, "source")
    rows = await session.execute(
        text(
            f"update question_sources s set {sets}, updated_at = now() where s.id = :id "
            f"returning {SOURCE_COLS}"
        ),
        {**params, "id": source_id},
    )
    return rows.mappings().one_or_none()


async def delete_source(session: AsyncSession, source_id: UUID) -> RowMapping | None:
    """Hard delete; questions and embeddings go with it via ON DELETE CASCADE. Returns
    {id, storage_key} of the deleted row (so the caller can remove the file) or None."""
    rows = await session.execute(
        text("delete from question_sources where id = :id returning id, storage_key"),
        {"id": source_id},
    )
    return rows.mappings().one_or_none()


async def set_embedding_status(session: AsyncSession, source_id: UUID, status: str) -> None:
    """status in pending|done|failed|skipped (else ValueError). Returns nothing."""
    if status not in EMBEDDING_STATUSES:
        raise ValueError(f"unknown embedding status {status!r}")
    await session.execute(
        text(
            "update question_sources set embedding_status = :status, updated_at = now() "
            "where id = :id"
        ),
        {"id": source_id, "status": status},
    )


# ---------------------------------------------------------------- questions


def question_rows(parsed: Iterable[ParsedQuestion]) -> list[dict]:
    """ParsedQuestion -> insert_questions row: {subject, number, level, topic, text,
    options: list[str], answer: int, warnings: list[str]}."""
    return [
        {
            "subject": q.subject,
            "number": q.number,
            "level": q.level,
            "topic": q.topic,
            "text": q.text,
            "options": list(q.options),
            "answer": q.answer,
            "warnings": list(q.warnings),
        }
        for q in parsed
    ]


async def insert_questions(session: AsyncSession, source_id: UUID, rows: Sequence[dict]) -> int:
    """Insert bank questions (rows from question_rows) as unapproved mcq, created_by = the
    source's uploader. Returns the number inserted (0 when rows is empty or the source is gone)."""
    if not rows:
        return 0
    for r in rows:
        _check_level(r["level"])
    result = await session.execute(
        text(
            "insert into questions (kind, body, options, answer, difficulty, ai_generated, "
            "approved, created_by, source_id, subject, topic, source_number, warnings) "
            "select cast('mcq' as question_kind), r.text, r.options, r.answer, "
            "cast(r.level as difficulty), false, false, s.uploaded_by, s.id, r.subject, "
            "r.topic, r.number, "
            "array(select jsonb_array_elements_text(r.warnings)) "
            "from question_sources s cross join jsonb_to_recordset(cast(:rows as jsonb)) as "
            "r(subject text, number int, level text, topic text, text text, options jsonb, "
            "answer jsonb, warnings jsonb) "
            "where s.id = :source_id returning id"
        ),
        {"source_id": source_id, "rows": _j(list(rows))},
    )
    return len(result.all())


async def delete_questions(session: AsyncSession, source_id: UUID) -> int:
    """Delete every question of a source (replace flow); embeddings cascade. Returns count."""
    result = await session.execute(
        text("delete from questions where source_id = :source_id returning id"),
        {"source_id": source_id},
    )
    return len(result.all())


async def list_questions(
    session: AsyncSession,
    source_id: UUID,
    *,
    subject: str | None = None,
    level: str | None = None,
    approved: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RowMapping], int]:
    """A page of a source's questions (QUESTION_COLS: id, source_id, subject, topic, level,
    text, options, answer, explanation, approved, warnings, source_number, created_at), ordered
    by subject then number, plus the total matching the filters. HR-only (includes answer)."""
    where = ["q.source_id = :source_id"]
    params: dict = {"source_id": source_id}
    if subject is not None:
        where.append("q.subject = :subject")
        params["subject"] = subject
    if level is not None:
        _check_level(level)
        where.append("cast(q.difficulty as text) = :level")
        params["level"] = level
    if approved is not None:
        where.append("q.approved = :approved")
        params["approved"] = approved
    cond = " and ".join(where)
    total = await session.execute(
        text(f"select count(*) as n from questions q where {cond}"), dict(params)
    )
    n = total.mappings().one()["n"]
    rows = await session.execute(
        text(
            f"select {QUESTION_COLS} from questions q where {cond} "
            "order by q.subject, q.source_number, q.id limit :limit offset :offset"
        ),
        {**params, "limit": limit, "offset": offset},
    )
    return list(rows.mappings().all()), n


async def counts(session: AsyncSession, source_id: UUID) -> dict[str, dict[str, dict[str, int]]]:
    """{subject: {level: {"total": int, "approved": int}}}; only cells that have questions."""
    rows = await session.execute(
        text(
            "select q.subject, cast(q.difficulty as text) as level, count(*) as total, "
            "count(*) filter (where q.approved) as approved "
            "from questions q where q.source_id = :source_id "
            "group by q.subject, q.difficulty order by q.subject, q.difficulty"
        ),
        {"source_id": source_id},
    )
    out: dict[str, dict[str, dict[str, int]]] = {}
    for r in rows.mappings().all():
        out.setdefault(r["subject"], {})[r["level"]] = {
            "total": int(r["total"]),
            "approved": int(r["approved"]),
        }
    return out


async def get_question(
    session: AsyncSession, question_id: UUID, *, for_update: bool = False
) -> RowMapping | None:
    """One question (QUESTION_COLS) or None. Bank and non-bank questions alike; the caller
    checks source_id matches the bank in the URL."""
    lock = " for update" if for_update else ""
    rows = await session.execute(
        text(f"select {QUESTION_COLS} from questions q where q.id = :id{lock}"),
        {"id": question_id},
    )
    return rows.mappings().one_or_none()


async def update_question(
    session: AsyncSession, question_id: UUID, fields: Mapping[str, object]
) -> RowMapping | None:
    """Set the given fields (keys of QUESTION_FIELDS: text, options, answer, level, subject,
    topic, explanation, approved, warnings). Returns the row (QUESTION_COLS) or None. Unknown
    fields -> ValueError. Does not touch the question's embedding (the caller re-embeds)."""
    if "level" in fields:
        _check_level(str(fields["level"]))
    sets, params = _set_clause(fields, QUESTION_FIELDS, "question")
    if "warnings" in params:
        params["warnings"] = list(params["warnings"])  # type: ignore[arg-type]
    rows = await session.execute(
        text(f"update questions q set {sets} where q.id = :id returning {QUESTION_COLS}"),
        {**params, "id": question_id},
    )
    return rows.mappings().one_or_none()


async def set_approval(
    session: AsyncSession,
    source_id: UUID,
    *,
    ids: Sequence[UUID] | None = None,
    subject: str | None = None,
    approved: bool,
    only_clean: bool = False,
) -> int:
    """Approve / unapprove a source's questions: all of them, or only `ids`, or one `subject`;
    only_clean=True skips questions that still carry parse warnings. Returns rows changed."""
    where = ["source_id = :source_id"]
    params: dict = {"source_id": source_id, "approved": approved}
    if ids is not None:
        where.append("id = any(cast(:ids as uuid[]))")
        params["ids"] = list(ids)
    if subject is not None:
        where.append("subject = :subject")
        params["subject"] = subject
    if only_clean:
        where.append("cardinality(warnings) = 0")
    result = await session.execute(
        text(
            "update questions set approved = :approved where "
            + " and ".join(where)
            + " returning id"
        ),
        params,
    )
    return len(result.all())


# ---------------------------------------------------------------- embeddings / retrieval


async def upsert_embeddings(session: AsyncSession, rows: Sequence[Mapping]) -> int:
    """rows: [{question_id, embedding: list[float], model}]; insert or replace. Returns count.
    The vector length must equal question_embeddings' dimension (settings.embedding_dim)."""
    if not rows:
        return 0
    payload = [
        {
            "question_id": str(r["question_id"]),
            "embedding": _vec(r["embedding"]),
            "model": r["model"],
        }
        for r in rows
    ]
    result = await session.execute(
        text(
            "insert into question_embeddings (question_id, embedding, model) "
            "select r.question_id, cast(r.embedding as vector), r.model "
            "from jsonb_to_recordset(cast(:rows as jsonb)) as "
            "r(question_id uuid, embedding text, model text) "
            "on conflict (question_id) do update set embedding = excluded.embedding, "
            "model = excluded.model returning question_id"
        ),
        {"rows": _j(payload)},
    )
    return len(result.all())


async def search_questions(
    session: AsyncSession,
    *,
    track: str,
    levels: Sequence[str],
    subjects: Sequence[str] | None = None,
    query_vec: Sequence[float] | None = None,
    per_cell: int,
    model: str | None = None,
) -> list[RowMapping]:
    """Approved questions of active, ready sources of `track`, at most `per_cell` per
    (subject, level). Ranked by cosine distance to query_vec when given (questions without an
    embedding last), else by (source created_at, id) - deterministic. Returns rows: id,
    source_id, subject, topic, level, text, options, answer, explanation, distance (float, or
    None without query_vec / embedding), ordered by subject, level, rank. `model` restricts
    which embeddings count (only with query_vec)."""
    for lv in levels:
        _check_level(lv)
    where = [
        "s.track = :track",
        "s.is_active",
        "s.status = 'ready'",
        "q.approved",
        "cast(q.difficulty as text) = any(cast(:levels as text[]))",
    ]
    params: dict = {"track": track, "levels": list(levels), "per_cell": per_cell}
    if subjects is not None:
        where.append("q.subject = any(cast(:subjects as text[]))")
        params["subjects"] = list(subjects)
    if query_vec is not None:
        model_cond = ""
        if model is not None:
            model_cond = " and e.model = :model"
            params["model"] = model
        join = f"left join question_embeddings e on e.question_id = q.id{model_cond} "
        distance = "(e.embedding <=> cast(:query_vec as vector))"
        order = f"{distance} asc nulls last, s.created_at, q.id"
        params["query_vec"] = _vec(query_vec)
    else:
        join, distance, order = "", "cast(null as double precision)", "s.created_at, q.id"
    rows = await session.execute(
        text(
            "select id, source_id, subject, topic, level, text, options, answer, explanation, "
            "distance from (select q.id, q.source_id, q.subject, q.topic, "
            "cast(q.difficulty as text) as level, q.body as text, q.options, q.answer, "
            f"q.explanation, {distance} as distance, "
            f"row_number() over (partition by q.subject, q.difficulty order by {order}) as rn "
            "from questions q join question_sources s on s.id = q.source_id "
            f"{join}where {' and '.join(where)}) ranked "
            "where rn <= :per_cell order by subject, level, rn"
        ),
        params,
    )
    return list(rows.mappings().all())


# ---------------------------------------------------------------- availability (Task 4)


async def available_counts(session: AsyncSession, track: str) -> list[RowMapping]:
    """Approved questions of active, ready sources of `track` (same filter as
    search_questions), counted per (subject, level): rows {subject, level, n}."""
    rows = await session.execute(
        text(
            "select q.subject, cast(q.difficulty as text) as level, count(*) as n "
            "from questions q join question_sources s on s.id = q.source_id "
            "where s.track = :track and s.is_active and s.status = 'ready' and q.approved "
            "group by q.subject, q.difficulty order by q.subject, q.difficulty"
        ),
        {"track": track},
    )
    return list(rows.mappings().all())


# ---------------------------------------------------------------- embedding jobs (Task 5)


async def question_texts(
    session: AsyncSession, source_id: UUID, *, for_update: bool = False
) -> list[RowMapping]:
    """Every question of a source as {id, text, options} (what gets embedded), ordered by id.
    for_update=True row-locks them so a concurrent edit waits until the caller commits."""
    lock = " for update" if for_update else ""
    rows = await session.execute(
        text(
            "select q.id, q.body as text, q.options from questions q "
            f"where q.source_id = :source_id order by q.id{lock}"
        ),
        {"source_id": source_id},
    )
    return list(rows.mappings().all())


async def set_embedding_status_if(
    session: AsyncSession, source_id: UUID, status: str, *, sha256: str
) -> bool:
    """Set embedding_status only while the source still holds the file `sha256` (a replace
    changes it), so a stale embedding job never overwrites the status of a newer upload.
    Returns whether a row was updated. Unknown status -> ValueError."""
    if status not in EMBEDDING_STATUSES:
        raise ValueError(f"unknown embedding status {status!r}")
    result = await session.execute(
        text(
            "update question_sources set embedding_status = :status, updated_at = now() "
            "where id = :id and sha256 = :sha256 returning id"
        ),
        {"id": source_id, "status": status, "sha256": sha256},
    )
    return len(result.all()) > 0


async def delete_embedding(session: AsyncSession, question_id: UUID) -> None:
    """Drop one question's vector (stale after an edit that could not be re-embedded)."""
    await session.execute(
        text("delete from question_embeddings where question_id = :id"), {"id": question_id}
    )

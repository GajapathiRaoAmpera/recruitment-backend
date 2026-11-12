"""Question-bank logic: upload, list/detail, questions, metadata, edit, approval, replace, delete,
download, availability.

Every HR / Super Admin may view, download and use every bank. Changing a bank (metadata, file,
questions, approval, delete) needs uploader == actor or a Super Admin (else 403 FORBIDDEN).

The uploaded PDF is untrusted: size-capped while reading, `%PDF-` magic required, parsed in a
worker thread with a page cap and a time bound, never executed, its text never sent to an LLM.
Files are written only after a successful parse and removed again if the transaction fails.
"""

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import threading
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import PureWindowsPath
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import AppError
from app.papers.schemas import LEVELS, QuestionEdit
from app.qbank import embeddings, parser, repo, storage
from app.qbank.parser import ParsedBank
from app.qbank.schemas import (
    ApprovalResult,
    ApprovalUpdate,
    Availability,
    BankQuestionOut,
    QuestionPage,
    SourceDetail,
    SourceOut,
    SourcePage,
    SourcePatch,
)

log = logging.getLogger(__name__)

ENTITY = "question_sources"
MAX_PAGES = 400
PARSE_TIMEOUT_S = 60.0
CHUNK = 1024 * 1024
PDF_MAGIC = b"%PDF-"
FILENAME_MAX = 255
FK_VIOLATION = "23503"
UNIQUE_VIOLATION = "23505"
PARSER_WORKERS = 2

BANK_IN_USE_DELETE = (
    "This question bank's questions are used by issued tests, so it cannot be deleted. "
    "Deactivate it instead to hide it from new papers."
)
BANK_IN_USE_REPLACE = (
    "This question bank's questions are used by issued tests, so its file cannot be replaced. "
    "Upload the new file as a new bank and deactivate this one."
)


class ParserSlots:
    """Non-blocking admission for the PDF parser pool: at most PARSER_WORKERS parses at once.
    A slot is released when the worker thread really finishes (not when the request times out),
    so a stuck parse keeps its slot and new uploads get 503 PARSER_BUSY instead of queueing."""

    def __init__(self, size: int):
        self.size = size
        self.in_use = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self.in_use >= self.size:
                return False
            self.in_use += 1
            return True

    def release(self, _: object = None) -> None:
        with self._lock:
            self.in_use = max(0, self.in_use - 1)


# dedicated pool: pdfplumber work never competes with the default executor (asyncio.to_thread,
# storage I/O, auth JWKS lookups)
PARSER_POOL = ThreadPoolExecutor(max_workers=PARSER_WORKERS, thread_name_prefix="qbank-parse")
PARSER = ParserSlots(PARSER_WORKERS)


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


def _json(value: Any) -> Any:
    """jsonb arrives decoded from asyncpg; tolerate a raw string as well."""
    return json.loads(value) if isinstance(value, str | bytes) else value


def _can_manage(actor: StaffUser, row: Mapping) -> bool:
    return actor.role == "super_admin" or row["uploaded_by"] == actor.id


def _out(actor: StaffUser, row: Mapping, **extra: Any) -> SourceOut:
    d = {k: v for k, v in dict(row).items() if k not in ("storage_key", "report")}
    return SourceOut.model_validate({**d, **extra, "can_manage": _can_manage(actor, row)})


def _totals(cells: Mapping[str, Mapping[str, Mapping[str, int]]]) -> tuple[int, int]:
    total = sum(c["total"] for levels in cells.values() for c in levels.values())
    approved = sum(c["approved"] for levels in cells.values() for c in levels.values())
    return total, approved


def _question(row: Mapping) -> BankQuestionOut:
    d = dict(row)
    d["options"] = _json(d["options"])
    d["answer"] = _json(d["answer"])
    d["warnings"] = list(d.get("warnings") or [])
    return BankQuestionOut.model_validate(d)


def _sqlstate(e: BaseException) -> str | None:
    """SQLSTATE of a DB error: sqlalchemy IntegrityError (.orig.sqlstate / pgcode) or a raw
    asyncpg exception (.sqlstate)."""
    for err in (e, getattr(e, "orig", None), e.__cause__):
        code = getattr(err, "sqlstate", None) or getattr(err, "pgcode", None)
        if isinstance(code, str):
            return code
    return None


async def _rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception as e:  # the original error matters more
        log.warning("rollback failed (%s)", type(e).__name__)


async def _discard(key: str) -> None:
    """Best-effort file removal (a leftover file is harmless; a failed request is not)."""
    try:
        await storage.delete(key)
    except Exception as e:
        log.warning("could not delete question-bank file (%s)", type(e).__name__)


async def _get(session: AsyncSession, source_id: UUID, *, lock: bool = False) -> Mapping:
    row = await repo.get_source(session, source_id, for_update=lock)
    if row is None:
        raise AppError(404, "NOT_FOUND", "Question bank not found")
    return row


async def _manageable(
    session: AsyncSession, actor: StaffUser, source_id: UUID, *, lock: bool = False
) -> Mapping:
    """The bank if the actor may change it: missing -> 404, someone else's -> 403.
    lock=True holds a row lock (select ... for update) until commit / rollback."""
    row = await _get(session, source_id, lock=lock)
    if not _can_manage(actor, row):
        raise AppError(
            403, "FORBIDDEN", "Only the uploader or a Super Admin can change this question bank"
        )
    return row


def _duplicate(existing: Mapping, *, same: bool = False) -> AppError:
    message = (
        "This bank already holds exactly this file."
        if same
        else f"This file is already uploaded as the question bank '{existing['name']}'."
    )
    return AppError(
        409,
        "DUPLICATE_FILE",
        message,
        {"existing_id": str(existing["id"]), "existing_name": existing["name"]},
    )


def _filename(name: str | None) -> str:
    base = PureWindowsPath(name or "").name.strip()  # handles both / and \ separators
    return base[:FILENAME_MAX] or "question-bank.pdf"


def download_name(filename: str) -> str:
    """ASCII-only, header-safe file name ending in .pdf."""
    stem = re.sub(r"\.pdf$", "", filename.strip(), flags=re.I)
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" ._")[:120]
    return f"{safe or 'question-bank'}.pdf"


# --------------------------------------------------------------------------------------------
# file intake
# --------------------------------------------------------------------------------------------


def max_bytes() -> int:
    return get_settings().qbank_max_mb * 1024 * 1024


async def read_upload(file: UploadFile) -> bytes:
    """The upload's bytes, read in chunks; 413 FILE_TOO_LARGE as soon as it passes the limit."""
    limit = max_bytes()
    parts: list[bytes] = []
    size = 0
    while chunk := await file.read(CHUNK):
        size += len(chunk)
        if size > limit:
            raise AppError(
                413,
                "FILE_TOO_LARGE",
                f"The PDF must be {get_settings().qbank_max_mb} MB or smaller",
                {"max_mb": get_settings().qbank_max_mb},
            )
        parts.append(chunk)
    return b"".join(parts)


def _check_pdf(data: bytes) -> str:
    """Magic check; returns the sha256 hex digest."""
    if not data.startswith(PDF_MAGIC):
        raise AppError(422, "NOT_A_PDF", "The file is not a PDF")
    return hashlib.sha256(data).hexdigest()


def _extract_and_parse(data: bytes) -> ParsedBank:
    return parser.parse_pages(parser.extract_pages(data, max_pages=MAX_PAGES))


async def _parse(data: bytes) -> ParsedBank:
    """Extract + parse on the dedicated parser pool, bounded in time. Both slots busy -> 503
    PARSER_BUSY at once; too slow -> 422 PDF_UNREADABLE (timeout); zero questions -> 422."""
    if not PARSER.try_acquire():
        raise AppError(
            503,
            "PARSER_BUSY",
            "Other question-bank PDFs are being read right now. Try again in a minute.",
        )
    try:
        job: Future = PARSER_POOL.submit(_extract_and_parse, data)
    except BaseException:
        PARSER.release()
        raise
    job.add_done_callback(PARSER.release)  # runs when the thread is done, even after a timeout
    try:
        bank = await asyncio.wait_for(asyncio.wrap_future(job), timeout=PARSE_TIMEOUT_S)
    except TimeoutError as e:
        raise AppError(
            422,
            "PDF_UNREADABLE",
            "The PDF took too long to read",
            {"reason": "timeout", "seconds": PARSE_TIMEOUT_S},
        ) from e
    if not bank.questions:
        raise AppError(
            422,
            "NO_QUESTIONS_FOUND",
            "No questions could be read from this PDF; nothing was saved.",
            {"warnings": bank.report.warnings[:20], "skipped": bank.report.skipped[:20]},
        )
    return bank


async def _ensure_new_hash(session: AsyncSession, digest: str, source_id: UUID | None) -> None:
    existing = await repo.get_source_by_hash(session, digest)
    if existing is not None:
        raise _duplicate(existing, same=existing["id"] == source_id)


def _report(bank: ParsedBank) -> dict:
    return dataclasses.asdict(bank.report)


def _schedule_embedding(background: BackgroundTasks, source_id: UUID) -> None:
    background.add_task(run_embedding, source_id)


async def run_embedding(source_id: UUID) -> None:
    """Background job (after commit): embed the bank's questions; never raises."""
    try:
        await embeddings.embed_source(source_id, embeddings.make_embedder())
    except Exception as e:
        log.warning("question bank %s: embedding job crashed (%s)", source_id, type(e).__name__)


# --------------------------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------------------------


async def create_source(
    session: AsyncSession,
    actor: StaffUser,
    background: BackgroundTasks,
    *,
    data: bytes,
    filename: str | None,
    name: str,
    track: str,
    notes: str | None,
) -> SourceOut:
    name = name.strip()
    notes = (notes or "").strip() or None
    if not name:
        raise AppError(422, "VALIDATION_ERROR", "name must not be blank")
    digest = _check_pdf(data)
    await _ensure_new_hash(session, digest, None)
    await _rollback(session)  # end the read-only transaction; don't hold it during the parse
    bank = await _parse(data)

    key = storage.new_key()
    await storage.save(key, data)
    fname = _filename(filename)
    try:
        await _ensure_new_hash(session, digest, None)  # re-check: another upload may have won
        row = await repo.insert_source(
            session,
            name=name,
            filename=fname,
            storage_key=key,
            sha256=digest,
            track=track,
            notes=notes,
            uploaded_by=actor.id,
            report=_report(bank),
        )
        count = await repo.insert_questions(session, row["id"], repo.question_rows(bank.questions))
        await audit(
            session,
            actor.id,
            "question_bank.created",
            ENTITY,
            row["id"],
            {
                "name": name,
                "filename": fname,
                "track": track,
                "sha256": digest,
                "questions": count,
                "warnings": len(bank.report.warnings),
            },
        )
        await session.commit()
    except BaseException as e:
        await _rollback(session)
        await _discard(key)
        if _sqlstate(e) == UNIQUE_VIOLATION:  # same file committed concurrently
            raise AppError(409, "DUPLICATE_FILE", "This file is already uploaded.") from e
        raise
    _schedule_embedding(background, row["id"])
    return _out(actor, row, question_count=count, approved_count=0)


async def list_sources(
    session: AsyncSession,
    actor: StaffUser,
    *,
    track: str | None,
    is_active: bool | None,
    page: int,
    page_size: int,
) -> SourcePage:
    rows = [
        r
        for r in await repo.list_sources(session)
        if track in (None, r["track"]) and is_active in (None, r["is_active"])
    ]
    start = (page - 1) * page_size
    items = [_out(actor, r) for r in rows[start : start + page_size]]
    return SourcePage(items=items, total=len(rows), page=page, page_size=page_size)


async def _detail(session: AsyncSession, actor: StaffUser, row: Mapping) -> SourceDetail:
    cells = await repo.counts(session, row["id"])
    total, approved = _totals(cells)
    base = _out(actor, row, question_count=total, approved_count=approved).model_dump()
    return SourceDetail.model_validate(
        {**base, "report": _json(row["report"]) or {}, "counts": cells}
    )


async def get_source(session: AsyncSession, actor: StaffUser, source_id: UUID) -> SourceDetail:
    return await _detail(session, actor, await _get(session, source_id))


async def list_questions(
    session: AsyncSession,
    source_id: UUID,
    *,
    subject: str | None,
    level: str | None,
    approved: bool | None,
    page: int,
    page_size: int,
) -> QuestionPage:
    await _get(session, source_id)
    rows, total = await repo.list_questions(
        session,
        source_id,
        subject=subject,
        level=level,
        approved=approved,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return QuestionPage(
        items=[_question(r) for r in rows], total=total, page=page, page_size=page_size
    )


async def availability(session: AsyncSession, track: str) -> Availability:
    subjects: dict[str, dict[str, int]] = {}
    by_level = dict.fromkeys(LEVELS, 0)
    for r in await repo.available_counts(session, track):
        n = int(r["n"])
        subjects.setdefault(r["subject"], dict.fromkeys(LEVELS, 0))[r["level"]] = n
        by_level[r["level"]] += n
    return Availability.model_validate(
        {
            "track": track,
            "subjects": subjects,
            "by_level": by_level,
            "total": sum(by_level.values()),
        }
    )


async def update_source(
    session: AsyncSession, actor: StaffUser, source_id: UUID, body: SourcePatch
) -> SourceDetail:
    before = await _manageable(session, actor, source_id, lock=True)
    fields = body.model_dump(include=body.model_fields_set)
    changed = {k: v for k, v in fields.items() if before[k] != v}
    row = before
    if changed:
        row = await repo.update_source(session, source_id, changed)
        if row is None:  # unreachable under the row lock; keep the contract explicit
            raise AppError(404, "NOT_FOUND", "Question bank not found")
        await audit(
            session,
            actor.id,
            "question_bank.updated",
            ENTITY,
            source_id,
            {"from": {k: before[k] for k in changed}, "to": changed},
        )
        await session.commit()
    return await _detail(session, actor, row)


async def replace_file(
    session: AsyncSession,
    actor: StaffUser,
    background: BackgroundTasks,
    source_id: UUID,
    *,
    data: bytes,
    filename: str | None,
) -> SourceDetail:
    await _manageable(session, actor, source_id)  # 404 / 403 before any parsing
    digest = _check_pdf(data)
    await _ensure_new_hash(session, digest, source_id)
    await _rollback(session)  # no transaction (or lock) held during the parse
    bank = await _parse(data)

    key = storage.new_key()
    await storage.save(key, data)
    fname = _filename(filename)
    try:
        before = await _manageable(session, actor, source_id, lock=True)
        await _ensure_new_hash(session, digest, source_id)
        removed = await repo.delete_questions(session, source_id)  # embeddings cascade
        count = await repo.insert_questions(session, source_id, repo.question_rows(bank.questions))
        row = await repo.update_source(
            session,
            source_id,
            {
                "filename": fname,
                "storage_key": key,
                "sha256": digest,
                "report": _report(bank),
                "status": "ready",
                "embedding_status": "pending",
            },
        )
        if row is None:  # unreachable under the row lock
            raise AppError(404, "NOT_FOUND", "Question bank not found")
        await audit(
            session,
            actor.id,
            "question_bank.replaced",
            ENTITY,
            source_id,
            {
                "from": {
                    "filename": before["filename"],
                    "sha256": before["sha256"],
                    "questions": removed,
                },
                "to": {"filename": fname, "sha256": digest, "questions": count},
            },
        )
        await session.commit()
    except BaseException as e:
        await _rollback(session)
        await _discard(key)  # the old file stays: the row still points at it
        if _sqlstate(e) == UNIQUE_VIOLATION:
            raise AppError(409, "DUPLICATE_FILE", "This file is already uploaded.") from e
        raise
    await _discard(before["storage_key"])
    _schedule_embedding(background, source_id)
    return await _detail(session, actor, row)


async def delete_source(session: AsyncSession, actor: StaffUser, source_id: UUID) -> None:
    before = await _manageable(session, actor, source_id, lock=True)
    try:
        row = await repo.delete_source(session, source_id)  # questions + embeddings cascade
        if row is None:  # unreachable under the row lock
            raise AppError(404, "NOT_FOUND", "Question bank not found")
        await audit(
            session,
            actor.id,
            "question_bank.deleted",
            ENTITY,
            source_id,
            {"name": before["name"], "filename": before["filename"], "sha256": before["sha256"]},
        )
        await session.commit()
    except Exception as e:
        if _sqlstate(e) != FK_VIOLATION:
            raise
        await _rollback(session)
        raise AppError(
            409,
            "BANK_IN_USE",
            "This question bank's questions are used by issued tests, so it cannot be deleted. "
            "Deactivate it instead to hide it from new papers.",
        ) from e
    await _discard(row["storage_key"])


async def download(session: AsyncSession, source_id: UUID) -> tuple[bytes, str]:
    row = await _get(session, source_id)
    data = await storage.read(row["storage_key"])
    if data is None:
        raise AppError(404, "FILE_NOT_FOUND", "The original PDF of this bank is missing")
    return data, download_name(row["filename"])


async def _reembed(session: AsyncSession, question_id: UUID) -> None:
    """Best effort, inside a savepoint: a failure never blocks the edit, and the stale vector
    never survives it (deleted when no fresh one could be stored)."""
    try:
        async with session.begin_nested():
            await embeddings.embed_question(session, question_id, embeddings.make_embedder())
    except Exception as e:
        log.warning("re-embedding question %s failed (%s)", question_id, type(e).__name__)
        await repo.delete_embedding(session, question_id)  # DB error here fails the edit


async def edit_question(
    session: AsyncSession,
    actor: StaffUser,
    source_id: UUID,
    question_id: UUID,
    body: QuestionEdit,
) -> BankQuestionOut:
    await _manageable(session, actor, source_id, lock=True)
    before = await repo.get_question(session, question_id, for_update=True)
    if before is None or before["source_id"] != source_id:
        raise AppError(404, "QUESTION_NOT_FOUND", "Question not found in this bank")
    after = {
        "text": body.text,
        "options": body.options,
        "answer": body.answer,
        "explanation": body.why,
    }
    row = await repo.update_question(session, question_id, after)
    if row is None:  # unreachable under the row lock
        raise AppError(404, "QUESTION_NOT_FOUND", "Question not found in this bank")
    await _reembed(session, question_id)
    old = {
        "text": before["text"],
        "options": _json(before["options"]),
        "answer": _json(before["answer"]),
        "explanation": before["explanation"],
    }
    await audit(
        session,
        actor.id,
        "question_bank.question_edited",
        ENTITY,
        source_id,
        {"question_id": str(question_id), "from": old, "to": after},
    )
    await session.commit()
    return _question(row)


async def set_approval(
    session: AsyncSession, actor: StaffUser, source_id: UUID, body: ApprovalUpdate
) -> ApprovalResult:
    await _manageable(session, actor, source_id, lock=True)
    if body.scope == "subject":
        if body.subject not in await repo.counts(session, source_id):
            raise AppError(422, "UNKNOWN_SUBJECT", "This bank has no questions in that subject.")
        changed = await repo.set_approval(
            session, source_id, subject=body.subject, approved=body.approved
        )
    elif body.scope == "ids":
        ids = list(dict.fromkeys(body.question_ids or []))
        changed = await repo.set_approval(session, source_id, ids=ids, approved=body.approved)
        if changed != len(ids):
            await _rollback(session)
            raise AppError(
                422,
                "UNKNOWN_QUESTIONS",
                "Some questions are not in this bank; nothing was changed.",
                {"requested": len(ids), "found": changed},
            )
    else:  # all_clean: approving skips questions with parse warnings; unapproving takes all
        changed = await repo.set_approval(
            session, source_id, approved=body.approved, only_clean=body.approved
        )
    total, approved = _totals(await repo.counts(session, source_id))
    await audit(
        session,
        actor.id,
        "question_bank.approval_changed",
        ENTITY,
        source_id,
        {
            "approved": body.approved,
            "scope": body.scope,
            "subject": body.subject,
            "question_ids": len(body.question_ids or []),
            "changed": changed,
        },
    )
    await session.commit()
    return ApprovalResult(changed=changed, question_count=total, approved_count=approved)

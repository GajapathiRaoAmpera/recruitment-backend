"""Paper builder logic: create + background generation, review, edit, accept, send."""

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.schemas import StaffUser
from app.core.audit import audit
from app.core.db import get_engine
from app.core.errors import AppError
from app.papers import agents, builder, repo
from app.papers import pool as pipeline
from app.papers.agents import OnProgress
from app.papers.schemas import (
    DraftDetail,
    DraftOut,
    IssueOut,
    OverlapOut,
    PaperSettings,
    PoolItem,
    PoolQuestion,
    QuestionEdit,
    SendResult,
)

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=repo.STALE_MINUTES)
TIMED_OUT = "Generation timed out"
GENERIC_FAILURE = "Generation failed. Try again."
SAMPLE_REF = "sample"
ENTITY = "paper_drafts"


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


def _json(value: Any) -> Any:
    """jsonb arrives decoded from asyncpg; tolerate a raw string as well."""
    return json.loads(value) if isinstance(value, str | bytes) else value


def _now() -> datetime:
    return datetime.now(UTC)


def _is_stale(row: Mapping) -> bool:
    created = row["created_at"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return row["status"] == "generating" and _now() - created > STALE_AFTER


def _effective(row: Mapping) -> dict:
    """Row as a plain dict, jsonb decoded, a stuck 'generating' job reported as failed."""
    d = dict(row)
    for key in ("settings", "checks", "progress", "pool"):
        if key in d:
            d[key] = _json(d[key])
    if _is_stale(row):
        d["status"], d["error"] = "failed", TIMED_OUT
    return d


def _out(row: Mapping) -> DraftOut:
    d = _effective(row)
    d.pop("pool", None)
    d.pop("checks", None)
    return DraftOut.model_validate(d)


def _pool(d: Mapping) -> list[PoolQuestion]:
    return [PoolQuestion.model_validate(q) for q in d.get("pool") or []]


def _selected(pool: list[PoolQuestion]) -> list[PoolQuestion]:
    return [q for q in pool if q.selected]


def _checks(settings: PaperSettings, drive_ref: str, pool: list[PoolQuestion]):
    """Checks run on what can reach students: HR's selection, not the whole pool."""
    return builder.run_checks(settings, drive_ref, _selected(pool))


def _detail(d: dict, issues: list[Mapping]) -> DraftDetail:
    settings = PaperSettings.model_validate(d["settings"])
    pool = _pool(d)
    chosen = _selected(pool)
    sample, overlap = [], None
    if chosen and d["status"] != "generating":
        sample = builder.build_paper(settings, d["drive_ref"], SAMPLE_REF, chosen).items
        a = builder.build_paper(settings, d["drive_ref"], "sample-a", chosen).items
        b = builder.build_paper(settings, d["drive_ref"], "sample-b", chosen).items
        ov = builder.overlap(a, b)
        overlap = OverlapOut(shared=ov.shared, same_position=ov.same_position, total=ov.total)
    base = {k: v for k, v in d.items() if k != "pool"}
    return DraftDetail.model_validate(
        {
            **base,
            "pool_size": len(pool),
            # recomputed (pure, cheap) so stored checks from older rules never disagree with accept
            "checks": [c.model_dump() for c in _checks(settings, d["drive_ref"], pool)]
            if chosen and d["status"] != "generating"
            else d.get("checks") or [],
            "sample": [i.model_dump() for i in sample],
            "overlap": overlap,
            "issues": [IssueOut.model_validate(dict(i)) for i in issues],
            "edited_count": sum(1 for q in pool if q.edited),
            "pool": [PoolItem.model_validate(q.model_dump()) for q in pool],
            "selected_count": len(chosen),
        }
    )


async def _load(
    session: AsyncSession, actor: StaffUser, draft_id: UUID, *, lock: bool = False
) -> dict:
    """The draft if the actor may see it. Other owners get the same 404 as a missing draft.

    lock=True takes a row lock (select ... for update) held until commit/rollback: every
    read -> check -> write mutation uses it, so two edits (or an edit racing an accept) on the
    same draft serialize instead of overwriting each other's pool.
    """
    row = await repo.get_draft(session, draft_id, for_update=lock)
    if row is None or (actor.role != "super_admin" and row["owner_id"] != actor.id):
        raise AppError(404, "NOT_FOUND", "Paper draft not found")
    return _effective(row)


def _seed(settings: PaperSettings, drive_ref: str, candidate_ref: str) -> str:
    # mirrors builder.build_paper so a paper can be rebuilt from the stored seed
    ref = candidate_ref if settings.different_per_student else "fixed"
    return f"{drive_ref}|{settings.track}|{ref}"


# --------------------------------------------------------------------------------------------
# AI providers + background generation
# --------------------------------------------------------------------------------------------


def ai_status() -> dict[str, bool]:
    return agents.providers_status()


# both tracks: Technical written and the sample composed by an NVIDIA model; HR verifies
REQUIRED_PROVIDERS = ("nvidia",)


def open_session() -> AsyncSession:
    """A session of its own for background work (the request session is closed by then)."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)()


async def build_pool(
    settings: PaperSettings,
    *,
    on_progress: OnProgress,
    overrides: Mapping[str, str] | None,
    session: AsyncSession,
    draft_id: UUID,
) -> list[PoolQuestion]:
    """The network step (see app.papers.pool). Tests monkeypatch this."""
    return await pipeline.build_pool(
        settings, on_progress=on_progress, overrides=overrides, draft_id=draft_id
    )


async def run_generation(draft_id: UUID, actor_id: UUID) -> None:
    """Background job: generating -> draft (pool + checks) | failed (safe message)."""
    try:
        async with open_session() as session:
            row = await repo.get_draft(session, draft_id)
            if row is None or row["status"] != "generating":
                return
            d = _effective(row)
            try:
                settings = PaperSettings.model_validate(d["settings"])

                async def on_progress(done: int, total: int) -> None:
                    await repo.update_progress(session, draft_id, done, total)
                    await session.commit()

                overrides = await repo.get_prompt_overrides(session)
                questions = await build_pool(
                    settings,
                    on_progress=on_progress,
                    overrides=overrides,
                    session=session,
                    draft_id=draft_id,
                )
                checks = _checks(settings, d["drive_ref"], questions)
                finished = await repo.finish_generation(
                    session,
                    draft_id,
                    [q.model_dump() for q in questions],
                    [c.model_dump() for c in checks],
                )
                if not finished:  # timed out (reported failed) or no longer generating
                    log.warning("paper draft %s finished too late; result dropped", draft_id)
                    await session.rollback()
                    return
                await audit(
                    session,
                    actor_id,
                    "paper_draft.generated",
                    ENTITY,
                    draft_id,
                    {"pool_size": len(questions), "checks_ok": all(c.ok for c in checks)},
                )
                await session.commit()
            except Exception as e:
                await session.rollback()
                # AppError messages are written for users; anything else (httpx, DB, bugs) may
                # carry URLs or internals, so only its type name is logged and a generic text kept
                message = e.message if isinstance(e, AppError) else GENERIC_FAILURE
                log.warning("paper draft %s generation failed (%s)", draft_id, type(e).__name__)
                if not await repo.fail_generation(session, draft_id, message):
                    await session.rollback()
                    return
                await audit(
                    session,
                    actor_id,
                    "paper_draft.generation_failed",
                    ENTITY,
                    draft_id,
                    {"error": message},
                )
                await session.commit()
    except Exception as e:  # never let a background job crash the worker
        log.error(
            "paper draft %s: could not record generation result (%s)", draft_id, type(e).__name__
        )


# --------------------------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------------------------


async def create_draft(
    session: AsyncSession,
    actor: StaffUser,
    background: BackgroundTasks,
    drive_ref: str,
    title: str,
    settings: PaperSettings,
) -> DraftOut:
    status = ai_status()
    missing = [p for p in REQUIRED_PROVIDERS if not status.get(p)]
    if missing:
        raise AppError(
            503,
            "AI_PROVIDER_NOT_CONFIGURED",
            "AI question generation is not configured on the server "
            f"(missing: {', '.join(m.upper() for m in missing)} API key).",
            {"providers": missing},
        )
    row = await repo.insert_draft(
        session,
        drive_ref=drive_ref,
        owner_id=actor.id,
        title=title,
        track=settings.track,
        settings=settings.model_dump(),
    )
    await audit(
        session,
        actor.id,
        "paper_draft.create",
        ENTITY,
        row["id"],
        {"drive_ref": drive_ref, "title": title, "settings": settings.model_dump()},
    )
    await session.commit()
    background.add_task(run_generation, row["id"], actor.id)
    return _out(row)


async def get_draft(session: AsyncSession, actor: StaffUser, draft_id: UUID) -> DraftDetail:
    d = await _load(session, actor, draft_id)
    return _detail(d, await repo.list_issues(session, draft_id))


async def list_drafts(session: AsyncSession, actor: StaffUser) -> list[DraftOut]:
    owner = None if actor.role == "super_admin" else actor.id
    return [_out(r) for r in await repo.list_drafts(session, owner)]


async def edit_question(
    session: AsyncSession,
    actor: StaffUser,
    draft_id: UUID,
    question_id: str,
    body: QuestionEdit,
) -> DraftDetail:
    d = await _load(session, actor, draft_id, lock=True)
    if d["status"] != "draft":
        raise AppError(
            409, "NOT_EDITABLE", "Questions can only be edited while the paper is a draft."
        )
    pool = _pool(d)
    index = next((i for i, q in enumerate(pool) if q.id == question_id), None)
    if index is None:
        raise AppError(404, "QUESTION_NOT_FOUND", "Question not found in this paper")
    before = pool[index]
    after = before.model_copy(update={**body.model_dump(), "edited": True})
    pool[index] = after
    settings = PaperSettings.model_validate(d["settings"])
    checks = _checks(settings, d["drive_ref"], pool)
    pool_json = [q.model_dump() for q in pool]
    checks_json = [c.model_dump() for c in checks]
    if not await repo.save_pool_and_checks(session, draft_id, pool_json, checks_json):
        raise AppError(
            409, "NOT_EDITABLE", "Questions can only be edited while the paper is a draft."
        )
    fields = ("text", "options", "answer", "why")
    await audit(
        session,
        actor.id,
        "paper_draft.question_edit",
        ENTITY,
        draft_id,
        {
            "question_id": question_id,
            "from": {f: getattr(before, f) for f in fields},
            "to": {f: getattr(after, f) for f in fields},
        },
    )
    await session.commit()
    d = {**d, "pool": pool_json, "checks": checks_json}
    return _detail(d, await repo.list_issues(session, draft_id))


async def set_selection(
    session: AsyncSession, actor: StaffUser, draft_id: UUID, selected_ids: list[str]
) -> DraftDetail:
    """HR picks which pool questions may reach students; checks re-run on the selection."""
    d = await _load(session, actor, draft_id, lock=True)
    if d["status"] != "draft":
        raise AppError(
            409, "NOT_EDITABLE", "Questions can only be selected while the paper is a draft."
        )
    pool = _pool(d)
    wanted = set(selected_ids)
    unknown = wanted - {q.id for q in pool}
    if unknown:
        raise AppError(
            422,
            "UNKNOWN_QUESTIONS",
            "Some selected questions are not in this paper.",
            {"unknown": sorted(unknown)[:20]},
        )
    pool = [q.model_copy(update={"selected": q.id in wanted}) for q in pool]
    settings = PaperSettings.model_validate(d["settings"])
    checks = _checks(settings, d["drive_ref"], pool)
    pool_json = [q.model_dump() for q in pool]
    checks_json = [c.model_dump() for c in checks]
    if not await repo.save_pool_and_checks(session, draft_id, pool_json, checks_json):
        raise AppError(
            409, "NOT_EDITABLE", "Questions can only be selected while the paper is a draft."
        )
    await audit(
        session,
        actor.id,
        "paper_draft.selection",
        ENTITY,
        draft_id,
        {"selected": len(wanted), "of": len(pool)},
    )
    await session.commit()
    d = {**d, "pool": pool_json, "checks": checks_json}
    return _detail(d, await repo.list_issues(session, draft_id))


async def accept_draft(session: AsyncSession, actor: StaffUser, draft_id: UUID) -> DraftOut:
    d = await _load(session, actor, draft_id, lock=True)
    if d["status"] == "accepted":
        raise AppError(409, "ALREADY_ACCEPTED", "This paper has already been accepted.")
    if d["status"] != "draft":
        raise AppError(409, "NOT_READY", "Only a generated draft can be accepted.")
    settings = PaperSettings.model_validate(d["settings"])
    checks = _checks(settings, d["drive_ref"], _pool(d))
    failed = [c.id for c in checks if not c.ok]
    if failed:
        raise AppError(
            409,
            "CHECKS_FAILED",
            "Fix the failed checks before accepting the paper.",
            {"failed": failed},
        )
    row = await repo.mark_accepted(session, draft_id)
    if row is None:  # someone else accepted it between our read and write
        raise AppError(409, "ALREADY_ACCEPTED", "This paper has already been accepted.")
    await audit(session, actor.id, "paper_draft.accept", ENTITY, draft_id, {"from": "draft"})
    await session.commit()
    return _out(row)


async def send_papers(
    session: AsyncSession, actor: StaffUser, draft_id: UUID, candidate_refs: list[str]
) -> SendResult:
    d = await _load(session, actor, draft_id, lock=True)
    refs = list(dict.fromkeys(r.strip() for r in candidate_refs if r and r.strip()))
    if not refs:
        raise AppError(422, "NO_CANDIDATES", "Choose at least one candidate to send the paper to.")
    if d["status"] != "accepted":
        raise AppError(409, "NOT_ACCEPTED", "Accept the paper before sending it.")
    settings = PaperSettings.model_validate(d["settings"])
    persisted = 0
    if d.get("persisted_at") is None:
        chosen = [q for q in d.get("pool") or [] if q.get("selected", True)]
        persisted = await repo.persist_questions(session, draft_id, chosen)
    rows = [{"candidate_ref": r, "seed": _seed(settings, d["drive_ref"], r)} for r in refs]
    sent = await repo.insert_issues(session, draft_id, rows)
    result = SendResult(sent=sent, already_sent=len(refs) - sent)
    await audit(
        session,
        actor.id,
        "paper_draft.send",
        ENTITY,
        draft_id,
        {"candidate_refs": refs, **result.model_dump(), "questions_persisted": persisted},
    )
    await session.commit()
    return result

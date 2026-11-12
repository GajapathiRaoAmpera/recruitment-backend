"""All SQL for the paper builder (drafts, issues, persisting questions at send)."""

import json
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

# list/summary columns; the pool is only selected by get_draft
COLS = (
    "id, drive_ref, owner_id, title, track, settings, checks, status, progress, error, "
    "jsonb_array_length(pool) as pool_size, created_at, accepted_at, persisted_at"
)

# a 'generating' draft older than this is reported failed; a later finish is a no-op
STALE_MINUTES = 15

# pool level -> questions.difficulty enum value
DIFFICULTY = {"easy": "easy", "medium": "medium", "advanced": "advanced"}


def _j(value: object) -> str:
    return json.dumps(value, default=str)


async def insert_draft(
    session: AsyncSession,
    *,
    drive_ref: str,
    owner_id: UUID,
    title: str,
    track: str,
    settings: dict,
) -> RowMapping:
    rows = await session.execute(
        text(
            "insert into paper_drafts (drive_ref, owner_id, title, track, settings, status) "
            "values (:drive_ref, :owner_id, :title, :track, cast(:settings as jsonb), "
            f"'generating') returning {COLS}"
        ),
        {
            "drive_ref": drive_ref,
            "owner_id": owner_id,
            "title": title,
            "track": track,
            "settings": _j(settings),
        },
    )
    return rows.mappings().one()


async def get_draft(
    session: AsyncSession, draft_id: UUID, *, for_update: bool = False
) -> RowMapping | None:
    """for_update=True row-locks the draft until the transaction ends (commit/rollback), so
    read -> check -> write mutations on one draft (edit, accept, send) run one at a time."""
    lock = " for update" if for_update else ""
    rows = await session.execute(
        text(f"select {COLS}, pool from paper_drafts where id = :id{lock}"), {"id": draft_id}
    )
    return rows.mappings().one_or_none()


async def list_drafts(session: AsyncSession, owner_id: UUID | None) -> list[RowMapping]:
    """owner_id None = every draft (Super Admin); otherwise only that owner's drafts (HR)."""
    if owner_id is None:
        rows = await session.execute(
            text(f"select {COLS} from paper_drafts order by created_at desc")
        )
    else:
        rows = await session.execute(
            text(
                f"select {COLS} from paper_drafts where owner_id = :owner order by created_at desc"
            ),
            {"owner": owner_id},
        )
    return list(rows.mappings().all())


async def update_progress(session: AsyncSession, draft_id: UUID, done: int, total: int) -> None:
    await session.execute(
        text(
            "update paper_drafts set progress = jsonb_build_object('done', cast(:done as int), "
            "'total', cast(:total as int)) where id = :id and status = 'generating'"
        ),
        {"id": draft_id, "done": done, "total": total},
    )


async def finish_generation(
    session: AsyncSession, draft_id: UUID, pool: list[dict], checks: list[dict]
) -> bool:
    rows = await session.execute(
        text(
            "update paper_drafts set pool = cast(:pool as jsonb), checks = cast(:checks as jsonb), "
            "status = 'draft', error = null "
            "where id = :id and status = 'generating' "
            "and created_at > now() - make_interval(mins => :stale_minutes) returning id"
        ),
        {"id": draft_id, "pool": _j(pool), "checks": _j(checks), "stale_minutes": STALE_MINUTES},
    )
    return rows.first() is not None


async def fail_generation(session: AsyncSession, draft_id: UUID, error: str) -> bool:
    rows = await session.execute(
        text(
            "update paper_drafts set status = 'failed', error = :error "
            "where id = :id and status = 'generating' returning id"
        ),
        {"id": draft_id, "error": error},
    )
    return rows.first() is not None


async def save_pool_and_checks(
    session: AsyncSession, draft_id: UUID, pool: list[dict], checks: list[dict]
) -> bool:
    """Atomic edit save; only while the draft is editable (status 'draft')."""
    rows = await session.execute(
        text(
            "update paper_drafts set pool = cast(:pool as jsonb), checks = cast(:checks as jsonb) "
            "where id = :id and status = 'draft' returning id"
        ),
        {"id": draft_id, "pool": _j(pool), "checks": _j(checks)},
    )
    return rows.first() is not None


async def mark_accepted(session: AsyncSession, draft_id: UUID) -> RowMapping | None:
    """draft -> accepted; None when the draft was not in 'draft' (lost a race, already done)."""
    rows = await session.execute(
        text(
            "update paper_drafts set status = 'accepted', accepted_at = now() "
            f"where id = :id and status = 'draft' returning {COLS}"
        ),
        {"id": draft_id},
    )
    return rows.mappings().one_or_none()


async def persist_questions(session: AsyncSession, draft_id: UUID, pool: Sequence[dict]) -> int:
    """Write the pool into `questions` (ids kept); never twice. Returns rows inserted."""
    items = [
        {
            "id": q["id"],
            "body": q["text"],
            "options": q["options"],
            "answer": q["answer"],
            "difficulty": DIFFICULTY[q["level"]],
            "section": q["section"],
            "style": q["style"],
            "explanation": q["why"],
        }
        for q in pool
    ]
    inserted = 0
    if items:
        rows = await session.execute(
            text(
                "insert into questions (id, kind, body, options, answer, tags, difficulty, "
                "ai_generated, approved, created_by, explanation) "
                "select r.id, cast('mcq' as question_kind), r.body, r.options, r.answer, "
                "array['paper:' || d.id::text, 'track:' || d.track, 'section:' || r.section, "
                "'style:' || r.style], cast(r.difficulty as difficulty), true, true, "
                "d.owner_id, r.explanation "
                "from paper_drafts d cross join jsonb_to_recordset(cast(:items as jsonb)) as "
                "r(id uuid, body text, options jsonb, answer jsonb, difficulty text, "
                "section text, style text, explanation text) "
                "where d.id = :id "
                "on conflict (id) do nothing returning id"
            ),
            {"id": draft_id, "items": _j(items)},
        )
        inserted = len(rows.all())
    await session.execute(
        text(
            "update paper_drafts set persisted_at = now() where id = :id and persisted_at is null"
        ),
        {"id": draft_id},
    )
    return inserted


async def insert_issues(session: AsyncSession, draft_id: UUID, rows: Sequence[dict]) -> int:
    """rows: [{candidate_ref, seed}]. Already-issued refs are kept as they are. Returns new."""
    if not rows:
        return 0
    result = await session.execute(
        text(
            "insert into paper_issues (draft_id, candidate_ref, seed) "
            "select cast(:id as uuid), r.candidate_ref, r.seed "
            "from jsonb_to_recordset(cast(:rows as jsonb)) as r(candidate_ref text, seed text) "
            "on conflict (draft_id, candidate_ref) do nothing returning candidate_ref"
        ),
        {"id": draft_id, "rows": _j(list(rows))},
    )
    return len(result.all())


async def list_issues(session: AsyncSession, draft_id: UUID) -> list[RowMapping]:
    rows = await session.execute(
        text(
            "select candidate_ref, seed, issued_at from paper_issues where draft_id = :id "
            "order by issued_at, candidate_ref"
        ),
        {"id": draft_id},
    )
    return list(rows.mappings().all())


async def get_prompt_overrides(session: AsyncSession, prefix: str = "paper_") -> dict[str, str]:
    """ai_prompts rows (key -> body) that override the paper builder's default prompts."""
    rows = await session.execute(
        text("select key, body from ai_prompts where starts_with(key, :prefix)"),
        {"prefix": prefix},
    )
    return {r["key"]: r["body"] for r in rows.mappings().all()}

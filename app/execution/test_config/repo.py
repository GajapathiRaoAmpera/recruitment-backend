import json
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.coding_questions.schemas import HIDDEN_CASES, SAMPLE_CASES

_TEST_COLS = "id, round_id, difficulty, duration_sec, question_count, settings, opens_at, closes_at"


async def get_round(
    session: AsyncSession, round_id: UUID, owner_id: UUID | None
) -> RowMapping | None:
    """A round of a drive this HR owns (`owner_id` None = Super Admin, no filter)."""
    rows = await session.execute(
        text(
            'select r.id, r.type, r."Duration" as duration_min from rounds r '
            "join drives d on d.id = r.drive_id "
            "where r.id = :id and (cast(:owner as uuid) is null or d.owner_id = :owner)"
        ),
        {"id": round_id, "owner": owner_id},
    )
    return rows.mappings().one_or_none()


async def ensure_test(session: AsyncSession, round_id: UUID, default_seconds: int) -> None:
    await session.execute(
        text(
            "insert into tests (round_id, duration_sec, question_count) values (:r, :s, 0) "
            "on conflict (round_id) do nothing"
        ),
        {"r": round_id, "s": default_seconds},
    )


async def update_test(session: AsyncSession, round_id: UUID, changes: dict) -> None:
    # keys come from TestConfigIn, never from raw input
    casts = {"difficulty": "cast(:difficulty as difficulty)"}
    sets = ", ".join(f"{k} = {casts.get(k, f':{k}')}" for k in changes)
    await session.execute(
        text(f"update tests set {sets} where round_id = :round_id"),
        {**changes, "round_id": round_id},
    )


async def get_test_of_round(session: AsyncSession, round_id: UUID) -> RowMapping:
    rows = await session.execute(
        text(f"select {_TEST_COLS} from tests where round_id = :r"), {"r": round_id}
    )
    return rows.mappings().one()


async def find_test_of_round(session: AsyncSession, round_id: UUID) -> RowMapping | None:
    """Read only: None when the round has no test row yet (never creates one)."""
    rows = await session.execute(
        text(f"select {_TEST_COLS} from tests where round_id = :r"), {"r": round_id}
    )
    return rows.mappings().one_or_none()


async def lock_test(
    session: AsyncSession, test_id: UUID, owner_id: UUID | None
) -> RowMapping | None:
    """The test, only if its drive is this HR's. Locked so a student starting at the same
    moment (which needs a share lock on this row) can't slip in mid-change."""
    rows = await session.execute(
        text(
            "select t.id, t.round_id, t.difficulty, t.duration_sec, t.question_count, "
            "t.settings, t.opens_at, t.closes_at from tests t "
            "join rounds r on r.id = t.round_id join drives d on d.id = r.drive_id "
            "where t.id = :id and (cast(:owner as uuid) is null or d.owner_id = :owner) "
            "for update of t"
        ),
        {"id": test_id, "owner": owner_id},
    )
    return rows.mappings().one_or_none()


async def has_attempts(session: AsyncSession, test_id: UUID) -> bool:
    rows = await session.execute(
        text("select 1 from attempts where test_id = :t limit 1"), {"t": test_id}
    )
    return rows.first() is not None


async def current_questions(session: AsyncSession, test_id: UUID) -> list[RowMapping]:
    rows = await session.execute(
        text(
            "select q.id as question_id, tq.position, q.difficulty, q.marks "
            "from test_questions tq join questions q on q.id = tq.question_id "
            "where tq.test_id = :t order by tq.position"
        ),
        {"t": test_id},
    )
    return list(rows.mappings())


async def usable_questions(
    session: AsyncSession, question_ids: list[UUID], owner_id: UUID | None
) -> list[RowMapping]:
    """Coding questions among `question_ids` that this HR may use (their own; Super Admin any),
    with whether each is approved and has exactly 3 sample + 15 hidden cases."""
    rows = await session.execute(
        text(
            "select q.id, q.difficulty, q.marks, q.approved, "
            "(select count(*) filter (where c.is_sample) = :samples "
            "        and count(*) filter (where not c.is_sample) = :hidden "
            " from test_cases c where c.question_id = q.id) as complete "
            "from questions q where q.id = any(:ids) and q.kind = 'coding' "
            "and (cast(:owner as uuid) is null or q.created_by = :owner)"
        ),
        {"ids": question_ids, "owner": owner_id, "samples": SAMPLE_CASES, "hidden": HIDDEN_CASES},
    )
    return list(rows.mappings())


async def replace_questions(session: AsyncSession, test_id: UUID, question_ids: list[UUID]) -> None:
    await session.execute(text("delete from test_questions where test_id = :t"), {"t": test_id})
    if question_ids:
        await session.execute(
            text(
                "insert into test_questions (test_id, question_id, position) values (:t, :q, :pos)"
            ),
            [{"t": test_id, "q": q, "pos": i} for i, q in enumerate(question_ids)],
        )


async def set_counts(
    session: AsyncSession, test_id: UUID, count: int, levels: dict[str, int]
) -> None:
    """question_count and the per-level counts HR sees in settings."""
    await session.execute(
        text(
            "update tests set question_count = :n, "
            "settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{levels}', "
            "cast(:levels as jsonb)) where id = :t"
        ),
        {"t": test_id, "n": count, "levels": json.dumps(levels)},
    )

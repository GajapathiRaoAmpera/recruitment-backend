from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.coding_questions.schemas import CaseIn

_COLS = "id, body, difficulty, tags, marks, approved, created_by, created_at"


async def insert_question(
    session: AsyncSession,
    owner_id: UUID,
    body: str,
    difficulty: str,
    tags: list[str],
    marks,
) -> RowMapping:
    # manual authoring: HR wrote it, so it is approved from the start
    rows = await session.execute(
        text(
            "insert into questions (kind, body, tags, difficulty, marks, approved, created_by) "
            "values ('coding', :body, cast(:tags as text[]), cast(:difficulty as difficulty), "
            f":marks, true, :owner) returning {_COLS}"
        ),
        {"body": body, "tags": tags, "difficulty": difficulty, "marks": marks, "owner": owner_id},
    )
    return rows.mappings().one()


async def insert_cases(session: AsyncSession, question_id: UUID, cases: list[CaseIn]) -> None:
    await session.execute(
        text(
            "insert into test_cases (question_id, input, expected, is_sample, position) "
            "values (:q, :input, :expected, :is_sample, :position)"
        ),
        [
            {
                "q": question_id,
                **c.model_dump(),
                "position": i,
            }  # samples-first is the caller's order
            for i, c in enumerate(cases)
        ],
    )


async def delete_cases(session: AsyncSession, question_id: UUID) -> None:
    await session.execute(text("delete from test_cases where question_id = :q"), {"q": question_id})


async def get_question(
    session: AsyncSession, question_id: UUID, owner_id: UUID | None
) -> RowMapping | None:
    """A coding question. `owner_id` None = no ownership filter (Super Admin)."""
    rows = await session.execute(
        text(
            f"select {_COLS} from questions where id = :id and kind = 'coding' "
            "and (cast(:owner as uuid) is null or created_by = :owner)"
        ),
        {"id": question_id, "owner": owner_id},
    )
    return rows.mappings().one_or_none()


async def get_cases(session: AsyncSession, question_id: UUID) -> list[RowMapping]:
    rows = await session.execute(
        text(
            "select id, input, expected, is_sample, position from test_cases "
            "where question_id = :q order by position"
        ),
        {"q": question_id},
    )
    return list(rows.mappings())


async def list_questions(
    session: AsyncSession,
    owner_id: UUID | None,
    difficulty: str | None,
    tags: list[str] | None,
    approved: bool | None,
    limit: int,
) -> list[RowMapping]:
    rows = await session.execute(
        text(
            f"select {_COLS} from questions where kind = 'coding' "
            "and (cast(:owner as uuid) is null or created_by = :owner) "
            "and (cast(:difficulty as text) is null "
            "or difficulty = cast(:difficulty as difficulty)) "
            "and (cast(:tags as text[]) is null or tags @> cast(:tags as text[])) "
            "and (cast(:approved as boolean) is null or approved = :approved) "
            "order by created_at desc limit :limit"
        ),
        {
            "owner": owner_id,
            "difficulty": difficulty,
            "tags": tags,
            "approved": approved,
            "limit": limit,
        },
    )
    return list(rows.mappings())


async def has_answers(session: AsyncSession, question_id: UUID) -> bool:
    rows = await session.execute(
        text("select 1 from answers where question_id = :q limit 1"), {"q": question_id}
    )
    return rows.first() is not None


async def update_question(session: AsyncSession, question_id: UUID, changes: dict) -> RowMapping:
    # keys come from CodingQuestionPatch, never from raw input
    casts = {"difficulty": "cast(:difficulty as difficulty)", "tags": "cast(:tags as text[])"}
    sets = ", ".join(f"{k} = {casts.get(k, f':{k}')}" for k in changes)
    rows = await session.execute(
        text(f"update questions set {sets} where id = :id returning {_COLS}"),
        {**changes, "id": question_id},
    )
    return rows.mappings().one()

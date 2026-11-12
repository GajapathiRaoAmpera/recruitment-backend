"""A drive's round pipeline: shortlist first, then the rounds students sit."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class RoundIn(BaseModel):
    type: Literal["shortlist", "aptitude", "coding", "ai_interview", "technical", "final"]
    mode: Literal["ai", "manual", "auto"] = "auto"
    threshold: float | None = Field(None, ge=0, le=100)  # cut-off %; minutes are set with the test


DEFAULT_ROUNDS = [
    RoundIn(type="shortlist"),
    RoundIn(type="aptitude", threshold=60),
    RoundIn(type="coding", threshold=50),
    RoundIn(type="ai_interview", mode="ai"),
    RoundIn(type="final", mode="manual"),
]


def check_rounds(rounds: list[RoundIn]) -> list[RoundIn]:
    types = [r.type for r in rounds]
    if len(rounds) < 2 or len(rounds) > 10:
        raise ValueError("A drive needs between 2 and 10 rounds")
    if types[0] != "shortlist" or types.count("shortlist") != 1:
        raise ValueError("The first round must be the resume shortlist, and only the first")
    if types.count("technical") > 1:
        raise ValueError("Only one extra technical round is allowed")
    return rounds


class RoundsIn(BaseModel):
    rounds: list[RoundIn] | None = None  # omitted = the standard pipeline

    @field_validator("rounds")
    @classmethod
    def _valid(cls, v):
        return check_rounds(v) if v is not None else v


async def create_rounds(session: AsyncSession, drive_id: UUID, rounds: list[RoundIn]) -> None:
    """Shortlist and the first test round start open (students invited by shortlisting can sign in);
    the rest wait until HR opens them."""
    for i, r in enumerate(rounds):
        await session.execute(
            text(
                "insert into rounds (drive_id, position, type, mode, status, threshold) values "
                "(:d, :pos, cast(:type as round_type), cast(:mode as round_mode), "
                "cast(:status as round_status), :threshold)"
            ),
            {
                "d": drive_id,
                "pos": i,
                "type": r.type,
                "mode": r.mode,
                "status": "active" if i <= 1 else "pending",
                "threshold": r.threshold,
            },
        )


async def ensure_rounds(session: AsyncSession, drive_id: UUID) -> None:
    """Drives made before rounds were saved get the standard pipeline when first needed."""
    found = await session.execute(text("select 1 from rounds where drive_id = :d"), {"d": drive_id})
    if found.first() is None:
        await create_rounds(session, drive_id, DEFAULT_ROUNDS)


async def first_test_round(session: AsyncSession, drive_id: UUID) -> UUID | None:
    result = await session.execute(
        text(
            "select id from rounds where drive_id = :d and type <> 'shortlist' "
            "order by position limit 1"
        ),
        {"d": drive_id},
    )
    return result.scalar_one_or_none()

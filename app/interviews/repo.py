import json
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_MINUTES = 40  # when HR left the round's "Duration" empty (35-45 is the expected range)

_MIN = 'coalesce(r."Duration", :def_min)'


def as_json(v: Any) -> Any:
    """jsonb may arrive as text from asyncpg."""
    return json.loads(v) if isinstance(v, str) else v


# ------------------------------------------------------------------------------ student side


async def find_for_sign_in(session: AsyncSession, student_id: str, round_id: UUID | None):
    """(candidate, AI interview round) pairs for this student ID, newest candidate first.
    Standalone on purpose: no invite, round status or earlier-round result is required."""
    result = await session.execute(
        text(
            "select c.id as candidate_id, c.dob, c.full_name, r.id as round_id, "
            f"{_MIN} as minutes, d.company, d.role_title "
            "from candidates c join drives d on d.id = c.drive_id "
            "join rounds r on r.drive_id = c.drive_id and r.type = 'ai_interview' "
            "where lower(c.student_id) = lower(:sid) "
            "and (cast(:rid as uuid) is null or r.id = :rid) "
            "order by c.created_at desc, r.position"
        ),
        {"sid": student_id.strip(), "rid": round_id, "def_min": DEFAULT_MINUTES},
    )
    return list(result.mappings())


async def ensure_interview(session: AsyncSession, round_id: UUID, candidate_id: UUID) -> UUID:
    await session.execute(
        text(
            "insert into interviews (round_id, candidate_id) values (:r, :c) "
            "on conflict (round_id, candidate_id) do nothing"
        ),
        {"r": round_id, "c": candidate_id},
    )
    result = await session.execute(
        text("select id from interviews where round_id = :r and candidate_id = :c"),
        {"r": round_id, "c": candidate_id},
    )
    return result.scalar_one()


async def context(session: AsyncSession, interview_id: UUID, candidate_id: UUID):
    """Everything the student routes need, scoped to the token's candidate."""
    result = await session.execute(
        text(
            "select i.id, i.status, i.started_at, i.round_id, i.candidate_id, "
            f"round({_MIN})::int as minutes, "
            f"i.started_at + make_interval(secs => {_MIN} * 60) as deadline_at, "
            "now() as server_now, d.company, d.role_title, d.jd_text, c.full_name, "
            "coalesce(rs.skills, '{}') as skills, "
            "coalesce(jsonb_array_length(i.transcript), 0) as turns "
            "from interviews i join rounds r on r.id = i.round_id "
            "join drives d on d.id = r.drive_id join candidates c on c.id = i.candidate_id "
            "left join resumes rs on rs.candidate_id = c.id "
            "where i.id = :i and i.candidate_id = :c"
        ),
        {"i": interview_id, "c": candidate_id, "def_min": DEFAULT_MINUTES},
    )
    return result.mappings().one_or_none()


async def mark_live(session: AsyncSession, interview_id: UUID) -> None:
    """The clock starts on the first session and never restarts."""
    await session.execute(
        text(
            "update interviews set status = 'live', started_at = coalesce(started_at, now()) "
            "where id = :i and status in ('scheduled', 'live')"
        ),
        {"i": interview_id},
    )


async def transcript(session: AsyncSession, interview_id: UUID) -> list[dict]:
    result = await session.execute(
        text("select transcript from interviews where id = :i"), {"i": interview_id}
    )
    return as_json(result.scalar_one_or_none()) or []


async def append_turns(
    session: AsyncSession, interview_id: UUID, turns: list[dict], max_total: int
) -> int | None:
    """Append in ONE statement (no lost update between two posts). None = refused: the interview
    isn't live, or the total would pass `max_total`."""
    result = await session.execute(
        text(
            "update interviews set transcript = "
            "coalesce(transcript, '[]'::jsonb) || cast(:t as jsonb) "
            "where id = :i and status = 'live' "
            "and coalesce(jsonb_array_length(transcript), 0) + :n <= :max "
            "returning jsonb_array_length(transcript)"
        ),
        {"i": interview_id, "t": json.dumps(turns, default=str), "n": len(turns), "max": max_total},
    )
    return result.scalar_one_or_none()


async def complete(session: AsyncSession, interview_id: UUID) -> bool:
    """live -> completed, with scoring marked pending. False if it wasn't live."""
    result = await session.execute(
        text(
            "update interviews set status = 'completed', ended_at = now(), "
            "ai_scores = cast(:p as jsonb) where id = :i and status = 'live' returning id"
        ),
        {"i": interview_id, "p": json.dumps({"pending": True})},
    )
    return result.first() is not None


async def save_scores(session: AsyncSession, interview_id: UUID, scores: dict) -> None:
    await session.execute(
        text("update interviews set ai_scores = cast(:s as jsonb) where id = :i"),
        {"i": interview_id, "s": json.dumps(scores, default=str)},
    )


async def prompt_overrides(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(
        text("select key, body from ai_prompts where key in ('interview_system', 'interview_eval')")
    )
    return {r["key"]: r["body"] for r in result.mappings()}


# ----------------------------------------------------------------------------------- HR side

_HR_COLS = (
    "select i.id, i.round_id, r.drive_id, d.company, d.role_title, c.id as candidate_id, "
    "c.student_id, c.full_name, i.status, i.started_at, i.ended_at, "
    "coalesce(jsonb_array_length(i.transcript), 0) as turns, i.ai_scores, "
    "(coalesce(i.hr_notes, '') <> '') as has_notes"
)
_HR_FROM = (
    " from interviews i join rounds r on r.id = i.round_id "
    "join drives d on d.id = r.drive_id join candidates c on c.id = i.candidate_id "
    "where (cast(:o as uuid) is null or d.owner_id = :o)"
)


async def list_for_hr(
    session: AsyncSession, owner: UUID | None, round_id: UUID | None, drive_id: UUID | None
) -> list[RowMapping]:
    result = await session.execute(
        text(
            _HR_COLS + _HR_FROM + " and (cast(:r as uuid) is null or i.round_id = :r) "
            "and (cast(:d as uuid) is null or r.drive_id = :d) "
            "order by (i.status = 'live') desc, i.started_at desc nulls last, c.full_name "
            "limit 500"
        ),
        {"o": owner, "r": round_id, "d": drive_id},
    )
    return list(result.mappings())


async def get_for_hr(session: AsyncSession, interview_id: UUID, owner: UUID | None):
    result = await session.execute(
        text(
            _HR_COLS + ", i.transcript, i.hr_notes, coalesce(rs.skills, '{}') as skills, "
            f"i.started_at + make_interval(secs => {_MIN} * 60) as deadline_at"
            + _HR_FROM.replace(" where", " left join resumes rs on rs.candidate_id = c.id where", 1)
            + " and i.id = :i"
        ),
        {"o": owner, "i": interview_id, "def_min": DEFAULT_MINUTES},
    )
    return result.mappings().one_or_none()


async def owned(session: AsyncSession, interview_id: UUID, owner: UUID | None):
    """id + status, only if the interview belongs to one of this HR's drives."""
    result = await session.execute(
        text(
            "select i.id, i.status, i.transcript is not null as has_transcript"
            + _HR_FROM
            + " and i.id = :i"
        ),
        {"o": owner, "i": interview_id},
    )
    return result.mappings().one_or_none()


async def set_notes(session: AsyncSession, interview_id: UUID, notes: str) -> None:
    await session.execute(
        text("update interviews set hr_notes = :n where id = :i"), {"i": interview_id, "n": notes}
    )


async def mark_pending(session: AsyncSession, interview_id: UUID) -> None:
    await save_scores(session, interview_id, {"pending": True})

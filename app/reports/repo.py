"""Read-only SQL for the drive report. get_drive checks ownership; the rest go by drive id."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

# Audit actions that tell the story of a drive; the rest (question edits, uploads) is noise here.
LOG_ACTIONS = (
    "drive.create",
    "drive.reassign",
    "candidate.import",
    "candidate.shortlist",
    "round.advance",
    "round.decide",
)


async def get_drive(session: AsyncSession, drive_id: UUID, owner: UUID | None) -> RowMapping | None:
    result = await session.execute(
        text(
            "select d.id, d.company, d.campus, d.role_title, d.status::text as status, "
            "d.created_at, u.full_name as owner_name, "
            "(select count(*) from candidates c where c.drive_id = d.id) as applied, "
            "(select count(*) from candidates c join resumes r on r.candidate_id = c.id "
            "where c.drive_id = d.id and r.shortlisted) as shortlisted "
            "from drives d left join users u on u.id = d.owner_id "
            "where d.id = :d and (cast(:o as uuid) is null or d.owner_id = :o)"
        ),
        {"d": drive_id, "o": owner},
    )
    return result.mappings().one_or_none()


async def rounds(session: AsyncSession, drive_id: UUID) -> list[RowMapping]:
    """Pipeline order, skipped rounds left out. first_at/last_at: when students sat it."""
    result = await session.execute(
        text(
            "select r.id, r.position, r.type::text as type, r.status::text as status, r.threshold, "
            "t.duration_sec, "
            "coalesce((select min(a.started_at) from attempts a where a.test_id = t.id), "
            "(select min(i.started_at) from interviews i where i.round_id = r.id)) as first_at, "
            "coalesce((select max(a.submitted_at) from attempts a where a.test_id = t.id), "
            "(select max(i.ended_at) from interviews i where i.round_id = r.id)) as last_at "
            "from rounds r left join tests t on t.round_id = r.id "
            "where r.drive_id = :d and r.status <> 'skipped' order by r.position"
        ),
        {"d": drive_id},
    )
    return list(result.mappings())


async def candidate_rows(session: AsyncSession, drive_id: UUID) -> list[RowMapping]:
    """One row per student per round, with their newest attempt's integrity signals."""
    result = await session.execute(
        text(
            "select rc.round_id, c.id as candidate_id, c.student_id, c.full_name, "
            "rc.status::text as status, rc.score, rc.rank, rc.hr_note, "
            "att.status::text as attempt_status, coalesce(att.tab_switches, 0) as tab_switches "
            "from round_candidates rc join rounds r on r.id = rc.round_id "
            "join candidates c on c.id = rc.candidate_id "
            "left join lateral (select a.status, a.tab_switches from attempts a "
            "join tests t on t.id = a.test_id where t.round_id = rc.round_id "
            "and a.candidate_id = c.id order by a.started_at desc nulls last limit 1) att on true "
            "where r.drive_id = :d order by r.position, c.full_name, c.student_id"
        ),
        {"d": drive_id},
    )
    return list(result.mappings())


async def decision_log(session: AsyncSession, drive_id: UUID) -> list[RowMapping]:
    result = await session.execute(
        text(
            "select l.action, l.entity_id, l.diff, l.created_at, u.full_name as actor "
            "from audit_logs l left join users u on u.id = l.actor_id "
            "where l.action = any(:actions) and ("
            "(l.entity = 'drive' and l.entity_id = :ds) or "
            "(l.entity = 'round' and l.entity_id in "
            "(select cast(r.id as text) from rounds r where r.drive_id = :d))) "
            "order by l.created_at"
        ),
        {"d": drive_id, "ds": str(drive_id), "actions": list(LOG_ACTIONS)},
    )
    return list(result.mappings())

import json
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_MINUTES = 30  # when HR left the round's "Duration" empty


async def get_startable(session: AsyncSession, candidate_id: UUID, round_id: UUID):
    """The round, only if it is open for this candidate to sit now."""
    result = await session.execute(
        text(
            'select r.id, r.type, r."Duration" as minutes from rounds r '
            "join round_candidates rc on rc.round_id = r.id "
            "where r.id = :r and rc.candidate_id = :c and r.status = 'active' "
            "and rc.status in ('invited', 'in_progress') "
            "and r.type in ('aptitude', 'coding', 'technical')"
        ),
        {"r": round_id, "c": candidate_id},
    )
    return result.mappings().one_or_none()


async def missing_steps(session: AsyncSession, candidate_id: UUID, round_id: UUID) -> list[str]:
    """Steps whose LATEST log is not ok (a location turned off later counts as missing)."""
    result = await session.execute(
        text(
            "select distinct on (step) step, ok from verification_logs "
            "where candidate_id = :c and round_id = :r "
            "and step in ('id_dob', 'location', 'terms', 'fullscreen') "
            "order by step, created_at desc"
        ),
        {"c": candidate_id, "r": round_id},
    )
    good = {r["step"] for r in result.mappings() if r["ok"]}
    return [s for s in ("id_dob", "location", "terms", "fullscreen") if s not in good]


async def ensure_test(session: AsyncSession, round_id: UUID, minutes: float | None) -> RowMapping:
    seconds = round((minutes or DEFAULT_MINUTES) * 60)
    await session.execute(
        text(
            "insert into tests (round_id, duration_sec, question_count) values (:r, :s, 0) "
            "on conflict (round_id) do nothing"
        ),
        {"r": round_id, "s": seconds},
    )
    result = await session.execute(
        text("select id, duration_sec from tests where round_id = :r"), {"r": round_id}
    )
    return result.mappings().one()


async def find_attempt(session: AsyncSession, round_id: UUID, candidate_id: UUID):
    """This candidate's attempt at this round, with whether its deadline has passed (DB clock)."""
    result = await session.execute(
        text(
            "select a.id, a.status, "
            "(a.deadline_at is not null and a.deadline_at < now()) as past_deadline "
            "from attempts a join tests t on t.id = a.test_id "
            "where t.round_id = :r and a.candidate_id = :c"
        ),
        {"r": round_id, "c": candidate_id},
    )
    return result.mappings().one_or_none()


async def start_attempt(
    session: AsyncSession, test_id: UUID, candidate_id: UUID, round_id: UUID, seconds: int
) -> UUID:
    """Create the attempt once. deadline_at is set here and never again: a repeat call finds the
    row and changes nothing (only a never-started row is filled in)."""
    await session.execute(
        text(
            "insert into attempts (test_id, candidate_id, status, started_at, deadline_at) "
            "values (:t, :c, 'in_progress', now(), now() + make_interval(secs => :s)) "
            "on conflict (test_id, candidate_id) do update set status = 'in_progress', "
            "started_at = now(), deadline_at = now() + make_interval(secs => :s) "
            "where attempts.status = 'not_started'"
        ),
        {"t": test_id, "c": candidate_id, "s": seconds},
    )
    await session.execute(
        text(
            "update round_candidates set status = 'in_progress' "
            "where round_id = :r and candidate_id = :c and status = 'invited'"
        ),
        {"r": round_id, "c": candidate_id},
    )
    result = await session.execute(
        text("select id from attempts where test_id = :t and candidate_id = :c"),
        {"t": test_id, "c": candidate_id},
    )
    return result.scalar_one()


async def has_questions(session: AsyncSession, attempt_id: UUID) -> bool:
    result = await session.execute(
        text("select 1 from attempt_questions where attempt_id = :a limit 1"), {"a": attempt_id}
    )
    return result.first() is not None


async def sent_paper(session: AsyncSession, round_id: UUID, candidate_id: UUID):
    """The newest accepted paper of the round's drive that HR sent to this candidate."""
    result = await session.execute(
        text(
            "select d.drive_ref, d.settings, d.pool from rounds r "
            "join paper_drafts d on d.drive_ref = cast(r.drive_id as text) "
            "join paper_issues pi on pi.draft_id = d.id and pi.candidate_ref = :c "
            "where r.id = :r and d.status = 'accepted' and d.persisted_at is not null "
            "order by d.created_at desc limit 1"
        ),
        {"r": round_id, "c": str(candidate_id)},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    out = dict(row)
    for key in ("settings", "pool"):  # jsonb may arrive as text; drive_ref is a plain string
        if isinstance(out[key], str):
            out[key] = json.loads(out[key])
    return out


async def link_questions(session: AsyncSession, attempt_id: UUID, rows: list[dict]) -> None:
    """rows: [{q: question id, p: position, o: option order}], this student's own paper."""
    await session.execute(
        text(
            "insert into attempt_questions (attempt_id, question_id, position, option_order) "
            "select :a, r.q, r.p, r.o from jsonb_to_recordset(cast(:rows as jsonb)) "
            "as r(q uuid, p int, o int[]) on conflict do nothing"
        ),
        {"a": attempt_id, "rows": json.dumps(rows)},
    )


async def coding_question_count(session: AsyncSession, round_id: UUID) -> int:
    """Coding questions on the round's test (0 when the test isn't set up yet)."""
    result = await session.execute(
        text(
            "select count(*) from tests t join test_questions tq on tq.test_id = t.id "
            "join questions q on q.id = tq.question_id and q.kind = 'coding' "
            "where t.round_id = :r"
        ),
        {"r": round_id},
    )
    return result.scalar_one()

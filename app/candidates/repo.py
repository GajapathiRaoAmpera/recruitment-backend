import json
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_drive(
    session: AsyncSession, drive_id: UUID, owner_id: UUID | None
) -> RowMapping | None:
    """The drive, or None. owner_id=None means any owner (Super Admin)."""
    result = await session.execute(
        text(
            "select id, jd_text from drives where id = :id "
            "and (cast(:o as uuid) is null or owner_id = :o)"
        ),
        {"id": drive_id, "o": owner_id},
    )
    return result.mappings().one_or_none()


async def drive_candidates(session: AsyncSession, drive_id: UUID) -> list[dict]:
    """Fields the resume matcher compares against."""
    result = await session.execute(
        text("select id, student_id, full_name, email, phone from candidates where drive_id = :d"),
        {"d": drive_id},
    )
    return [dict(r) for r in result.mappings()]


async def candidate_in_drive(session: AsyncSession, drive_id: UUID, candidate_id: UUID) -> bool:
    result = await session.execute(
        text("select 1 from candidates where id = :c and drive_id = :d"),
        {"c": candidate_id, "d": drive_id},
    )
    return result.first() is not None


async def upsert_resume(
    session: AsyncSession, candidate_id: UUID, storage_path: str, parsed: dict
) -> None:
    # shortlisted is left alone on re-upload: HR's decision survives a re-parse
    await session.execute(
        text(
            "insert into resumes (candidate_id, storage_path, extracted_text, skills, cgpa, "
            "jd_match, parse_status) values (:cid, :path, :text, :skills, :cgpa, :match, :status) "
            "on conflict (candidate_id) do update set storage_path = excluded.storage_path, "
            "extracted_text = excluded.extracted_text, skills = excluded.skills, "
            "cgpa = excluded.cgpa, jd_match = excluded.jd_match, "
            "parse_status = excluded.parse_status"
        ),
        {
            "cid": candidate_id,
            "path": storage_path,
            "text": parsed["text"],
            "skills": parsed["skills"],
            "cgpa": parsed["cgpa"],
            "match": parsed["jd_match"],
            "status": parsed["parse_status"],
        },
    )


async def list_candidates(
    session: AsyncSession,
    drive_id: UUID,
    skills: list[str],
    min_cgpa: float | None,
    min_match: float | None,
    shortlisted: bool | None,
    limit: int,
    offset: int,
) -> list[RowMapping]:
    # clauses are fixed strings; only values are bound
    where = ["c.drive_id = :d"]
    params: dict = {"d": drive_id, "limit": limit + 1, "offset": offset}
    if skills:
        where.append("r.skills @> cast(:skills as text[])")
        params["skills"] = skills
    if min_cgpa is not None:
        where.append("r.cgpa >= :min_cgpa")
        params["min_cgpa"] = min_cgpa
    if min_match is not None:
        where.append("r.jd_match >= :min_match")
        params["min_match"] = min_match
    if shortlisted is not None:
        where.append(
            "r.shortlisted is not distinct from :sl" if shortlisted is False else "r.shortlisted"
        )
        if shortlisted is False:
            params["sl"] = False
    result = await session.execute(
        text(
            "select c.id, c.student_id, c.full_name, c.email, r.cgpa, "
            "coalesce(r.skills, '{}') as skills, r.jd_match, "
            "coalesce(r.parse_status, 'missing') as parse_status, r.shortlisted "
            "from candidates c left join resumes r on r.candidate_id = c.id "
            f"where {' and '.join(where)} "
            "order by r.jd_match desc nulls last, c.student_id limit :limit offset :offset"
        ),
        params,
    )
    return list(result.mappings())


async def set_shortlisted(
    session: AsyncSession, drive_id: UUID, candidate_ids: list[UUID], value: bool
) -> int:
    result = await session.execute(
        text(
            "update resumes set shortlisted = :v where candidate_id = any(:ids) "
            "and candidate_id in (select id from candidates where drive_id = :d)"
        ),
        {"v": value, "ids": candidate_ids, "d": drive_id},
    )
    return result.rowcount


async def get_profile(
    session: AsyncSession, candidate_id: UUID, owner_id: UUID | None
) -> dict | None:
    """Candidate + resume + per-round history, only if HR owns the candidate's drive.
    dob and extracted_text are left out on purpose (login secret / bulky)."""
    result = await session.execute(
        text(
            "select c.id, c.drive_id, c.student_id, c.full_name, c.email, c.phone, c.form_data, "
            "r.storage_path, r.skills, r.cgpa, r.jd_match, r.parse_status, r.shortlisted "
            "from candidates c join drives d on d.id = c.drive_id "
            "left join resumes r on r.candidate_id = c.id "
            "where c.id = :c and (cast(:o as uuid) is null or d.owner_id = :o)"
        ),
        {"c": candidate_id, "o": owner_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    rounds = await session.execute(
        text(
            "select r.id as round_id, r.position, r.type, rc.status, rc.score, rc.rank, rc.hr_note "
            "from round_candidates rc join rounds r on r.id = rc.round_id "
            "where rc.candidate_id = :c order by r.position"
        ),
        {"c": candidate_id},
    )
    return {**row, "rounds": [dict(x) for x in rounds.mappings()]}


async def upsert_candidate(session: AsyncSession, drive_id: UUID, c: dict) -> bool:
    """True if inserted, False if an existing (drive, student_id) row was updated."""
    result = await session.execute(
        text(
            "insert into candidates "
            "(drive_id, student_id, dob, full_name, email, phone, form_data) "
            "values (:d, :student_id, :dob, :full_name, :email, :phone, cast(:form as jsonb)) "
            "on conflict (drive_id, student_id) do update set dob = excluded.dob, "
            "full_name = excluded.full_name, email = excluded.email, phone = excluded.phone, "
            "form_data = excluded.form_data returning (xmax = 0) as inserted"
        ),
        {"d": drive_id, **c, "form": json.dumps(c["form_data"], default=str)},
    )
    return result.scalar_one()


async def candidates_to_fetch(session: AsyncSession, drive_id: UUID, refetch: bool) -> list[dict]:
    result = await session.execute(
        text(
            "select c.id, c.student_id, c.form_data from candidates c "
            "left join resumes r on r.candidate_id = c.id "
            "where c.drive_id = :d and (:refetch or r.id is null) order by c.student_id"
        ),
        {"d": drive_id, "refetch": refetch},
    )
    return [dict(r) for r in result.mappings()]


async def sync_round_one(
    session: AsyncSession,
    drive_id: UUID,
    round_id: UUID,
    candidate_ids: list[UUID],
    shortlisted: bool,
) -> int:
    """Shortlisted candidates (that have a resume row) are invited to Round 1; un-shortlisted ones
    are taken out again, but only while still `invited` (never after they started)."""
    if shortlisted:
        result = await session.execute(
            text(
                "insert into round_candidates (round_id, candidate_id) "
                "select :r, c.id from candidates c join resumes s on s.candidate_id = c.id "
                "where c.drive_id = :d and c.id = any(:ids) on conflict do nothing"
            ),
            {"r": round_id, "d": drive_id, "ids": candidate_ids},
        )
    else:
        result = await session.execute(
            text(
                "delete from round_candidates where round_id = :r and candidate_id = any(:ids) "
                "and status = 'invited' and candidate_id in "
                "(select id from candidates where drive_id = :d)"
            ),
            {"r": round_id, "d": drive_id, "ids": candidate_ids},
        )
    return result.rowcount

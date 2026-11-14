import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_state(session: AsyncSession, attempt_id: UUID, candidate_id: UUID):
    result = await session.execute(
        text(
            "select id, test_id, status, deadline_at, now() as server_now, tab_switches, "
            "remaining_sec_on_lock, "
            "(status = 'in_progress' and deadline_at < now()) as past_deadline "
            "from attempts where id = :a and candidate_id = :c"
        ),
        {"a": attempt_id, "c": candidate_id},
    )
    return result.mappings().one_or_none()


async def questions_for(session: AsyncSession, attempt_id: UUID) -> list[dict]:
    """This student's questions in their order, options in their order. Never `answer`."""
    result = await session.execute(
        text(
            "select q.id, q.body, q.marks, (select jsonb_agg(q.options -> u.o order by u.ord) "
            "from unnest(aq.option_order) with ordinality as u(o, ord)) as options "
            "from attempt_questions aq join questions q on q.id = aq.question_id "
            "where aq.attempt_id = :a and q.kind = 'mcq' order by aq.position"
        ),
        {"a": attempt_id},
    )
    return [dict(r) for r in result.mappings()]


async def coding_questions_for(session: AsyncSession, test_id: UUID) -> list[dict]:
    """The test's coding questions with their SAMPLE cases only (max 3). Hidden cases
    (`is_sample = false`) are never selected here."""
    result = await session.execute(
        text(
            "select q.id, q.body, q.marks, q.mode, q.signature from test_questions tq "
            "join questions q on q.id = tq.question_id "
            "where tq.test_id = :t and q.kind = 'coding' order by tq.position"
        ),
        {"t": test_id},
    )
    questions = [dict(r) | {"samples": []} for r in result.mappings()]
    if questions:
        cases = await session.execute(
            text(
                "select question_id, input, expected from test_cases "
                "where question_id = any(cast(:ids as uuid[])) and is_sample "
                "order by question_id, position"
            ),
            {"ids": [q["id"] for q in questions]},
        )
        by_q = {q["id"]: q["samples"] for q in questions}
        for c in cases.mappings():
            if len(by_q[c["question_id"]]) < 3:
                by_q[c["question_id"]].append({"input": c["input"], "expected": c["expected"]})
    return questions


async def saved_answers(session: AsyncSession, attempt_id: UUID) -> dict[str, int | dict]:
    result = await session.execute(
        text("select question_id, response, code, language from answers where attempt_id = :a"),
        {"a": attempt_id},
    )
    saved: dict[str, int | dict] = {}
    for r in result.mappings():
        if r["code"] is not None:
            saved[str(r["question_id"])] = {"code": r["code"], "language": r["language"]}
        elif r["response"] is not None:
            saved[str(r["question_id"])] = r["response"]
    return saved


async def save_answer(
    session: AsyncSession, attempt_id: UUID, question_id: UUID, response: int
) -> str:
    """'ok', or why not: 'no_question', 'bad_option', 'closed' (locked/submitted), 'expired'."""
    row = await session.execute(
        text(
            "select cardinality(aq.option_order) as n, a.status, (a.deadline_at < now()) as past, "
            "(a.status = 'submitted' and a.submitted_at >= a.deadline_at) as auto "
            "from attempts a join attempt_questions aq on aq.attempt_id = a.id "
            "join questions q on q.id = aq.question_id and q.kind = 'mcq' "
            "where a.id = :a and q.id = :q"
        ),
        {"a": attempt_id, "q": question_id},
    )
    found = row.mappings().one_or_none()
    if found is None:
        return "no_question"
    if found["auto"]:  # the deadline submitted it, not the student
        return "expired"
    if found["status"] != "in_progress":
        return "closed"
    if found["past"]:
        return "expired"
    if not 0 <= response < found["n"]:
        return "bad_option"
    await session.execute(
        text(
            "insert into answers (attempt_id, question_id, response, updated_at) "
            "values (:a, :q, cast(:r as jsonb), now()) "
            "on conflict (attempt_id, question_id) do update set response = excluded.response, "
            "updated_at = now()"
        ),
        {"a": attempt_id, "q": question_id, "r": json.dumps(response)},
    )
    return "ok"


async def submit(session: AsyncSession, attempt_id: UUID):
    """Finalize: status 'submitted', submitted_at, and the time taken (capped at the deadline, so an
    auto-submit that arrives late is not counted as extra time). MCQs are scored in the same
    transaction. Returns None if the attempt isn't in progress (locked, or already submitted)."""
    result = await session.execute(
        text(
            "update attempts set status = 'submitted', submitted_at = now(), "
            "time_taken_sec = greatest(0, extract(epoch from "
            "(least(now(), deadline_at) - started_at))::int) "
            "where id = :a and status = 'in_progress' "
            "returning time_taken_sec, submitted_at"
        ),
        {"a": attempt_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    await session.execute(
        text(
            "update answers a set marks = case when "
            "to_jsonb(aq.option_order[(a.response #>> '{}')::int + 1]) = q.answer "
            "then q.marks else 0 end from questions q, attempt_questions aq "
            "where a.attempt_id = :a and q.id = a.question_id and q.kind = 'mcq' "
            "and aq.attempt_id = a.attempt_id and aq.question_id = a.question_id"
        ),
        {"a": attempt_id},
    )
    await session.execute(
        text(
            "update attempts set marks = coalesce("
            "(select sum(marks) from answers where attempt_id = :a), 0) where id = :a"
        ),
        {"a": attempt_id},
    )
    return row


async def current_status(session: AsyncSession, attempt_id: UUID):
    result = await session.execute(
        text("select status, time_taken_sec, submitted_at from attempts where id = :a"),
        {"a": attempt_id},
    )
    return result.mappings().one_or_none()

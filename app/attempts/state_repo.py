import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.attempts.temp_questions import QUESTIONS, TAG


async def attach_temp_questions(session: AsyncSession, test_id: UUID) -> int:
    """Give a question-less test the temporary questions. 0 = the test already has questions."""
    has = await session.execute(
        text("select 1 from test_questions where test_id = :t limit 1"), {"t": test_id}
    )
    if has.first() is not None:
        return 0
    for position, q in enumerate(QUESTIONS):
        await session.execute(
            text(
                "insert into questions (id, kind, body, options, answer, tags, difficulty, "
                "marks, approved) values (:id, 'mcq', :body, cast(:options as jsonb), "
                "cast(:answer as jsonb), cast(:tags as text[]), 'easy', 1, true) "
                "on conflict (id) do nothing"
            ),
            {
                "id": q["id"],
                "body": q["body"],
                "options": json.dumps(q["options"]),
                "answer": json.dumps(q["answer"]),
                "tags": [TAG],
            },
        )
        await session.execute(
            text(
                "insert into test_questions (test_id, question_id, position) "
                "values (:t, :q, :p) on conflict do nothing"
            ),
            {"t": test_id, "q": q["id"], "p": position},
        )
    await session.execute(
        text("update tests set question_count = :n where id = :t"),
        {"n": len(QUESTIONS), "t": test_id},
    )
    return len(QUESTIONS)


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


async def questions_for(session: AsyncSession, test_id: UUID) -> list[dict]:
    """Question text and options only. `answer` is never selected here."""
    result = await session.execute(
        text(
            "select q.id, q.body, q.options, q.marks from test_questions tq "
            "join questions q on q.id = tq.question_id "
            "where tq.test_id = :t and q.kind = 'mcq' order by tq.position"
        ),
        {"t": test_id},
    )
    return [dict(r) for r in result.mappings()]


async def saved_answers(session: AsyncSession, attempt_id: UUID) -> dict[str, int]:
    result = await session.execute(
        text("select question_id, response from answers where attempt_id = :a"),
        {"a": attempt_id},
    )
    return {
        str(r["question_id"]): r["response"] for r in result.mappings() if r["response"] is not None
    }


async def save_answer(
    session: AsyncSession, attempt_id: UUID, question_id: UUID, response: int
) -> str:
    """'ok', or why not: 'no_question', 'bad_option', 'closed' (locked/submitted), 'expired'."""
    row = await session.execute(
        text(
            "select q.options, a.status, (a.deadline_at < now()) as past from attempts a "
            "join test_questions tq on tq.test_id = a.test_id "
            "join questions q on q.id = tq.question_id and q.kind = 'mcq' "
            "where a.id = :a and q.id = :q"
        ),
        {"a": attempt_id, "q": question_id},
    )
    found = row.mappings().one_or_none()
    if found is None:
        return "no_question"
    if found["status"] != "in_progress":
        return "closed"
    if found["past"]:
        return "expired"
    if not 0 <= response < len(found["options"] or []):
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
            "update answers a set marks = case when a.response = q.answer then q.marks else 0 end "
            "from questions q where a.attempt_id = :a and q.id = a.question_id and q.kind = 'mcq'"
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

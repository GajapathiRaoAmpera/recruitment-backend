"""End to end: HR sets up a coding test, a student sits it, HR reads the result.

Student path: entry/start -> GET /attempt (coding question, samples only) -> run -> submit-code
(partial, then full marks) -> POST /attempt/submit -> attempts.marks includes the coding marks ->
run / submit-code are refused with 409 ATTEMPT_LOCKED.

INTEGRATION test against a real Postgres and a live Judge0, skipped by default. It never touches
the database in .env: it needs `E2E_DATABASE_URL` pointing at a scratch database that has been
migrated (`DATABASE_URL=<that url> uv run alembic upgrade head`), and `make judge0-up`.

    E2E_DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:55432/e2e make test-judge0

Everything goes through the real HTTP routes (only the HR login is stubbed; the student uses
the real entry token and attempt token). It removes the rows it created.
"""

import asyncio
import os
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.main import app
from tests.execution.test_reference_solutions import SOLUTIONS, sum_cases

pytestmark = pytest.mark.integration

DB_URL = os.environ.get("E2E_DATABASE_URL")
API = "/api/v1"
HR = StaffUser(
    id=uuid.uuid4(), email=f"e2e-{uuid.uuid4().hex[:8]}@x.com", full_name="E2E HR", role="hr"
)
OTHER_HR = StaffUser(
    id=uuid.uuid4(), email=f"e2e-{uuid.uuid4().hex[:8]}@x.com", full_name="E2E HR2", role="hr"
)
STUDENT_ID = f"E2E-{uuid.uuid4().hex[:8]}"
DOB = "2003-05-17"
HIDDEN = "SECRET-HIDDEN-EXPECTED"


def question_body(hidden_marker: str | None = None) -> dict:
    cases = [
        {"input": c.input, "expected": c.expected, "is_sample": c.is_sample} for c in sum_cases()
    ]
    return {
        "kind": "coding",
        "body": "Sum of numbers: line 1 is n, line 2 has n integers; print their sum.",
        "difficulty": "medium",
        "tags": ["e2e"],
        "marks": 10,
        "test_cases": cases,
    }


async def answer_marks(sessions, attempt_id: str, qid: str) -> Decimal | None:
    async with sessions() as s:
        return (
            await s.execute(
                text("select marks from answers where attempt_id = :a and question_id = :q"),
                {"a": uuid.UUID(attempt_id), "q": uuid.UUID(qid)},
            )
        ).scalar_one_or_none()


async def put_draft(c: httpx.AsyncClient, me: dict, qid: str, code: str) -> httpx.Response:
    return await c.put(
        f"{API}/attempt/code-drafts/{qid}", json={"code": code, "language": "python"}, headers=me
    )


async def drafts(c: httpx.AsyncClient, me: dict) -> dict[str, str]:
    r = await c.get(f"{API}/attempt/code-drafts", headers=me)
    assert r.status_code == 200, r.text
    return {q: d["code"] for q, d in r.json()["drafts"].items()}


async def _story(engine) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    who = {"user": HR}

    async def get_session_override():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = get_session_override
    app.dependency_overrides[security.current_user] = lambda: who["user"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://e2e") as c:
        # --- HR side -------------------------------------------------------------------------
        r = await c.post(
            f"{API}/drives",
            json={
                "company": "E2E Co",
                "campus": "Test Campus",
                "role_title": "Dev",
                "jd_text": "x",
                "rounds": [
                    {"type": "shortlist"},
                    {"type": "coding", "threshold": 50, "duration_min": 5},
                ],
            },
        )
        assert r.status_code == 201, r.text
        drive_id = r.json()["id"]
        async with sessions() as s:
            round_id = (
                await s.execute(
                    text("select id from rounds where drive_id = :d and type = 'coding'"),
                    {"d": uuid.UUID(drive_id)},
                )
            ).scalar_one()
            cand_id = (
                await s.execute(
                    text(
                        "insert into candidates (drive_id, student_id, dob, full_name, email) "
                        "values (:d, :sid, :dob, 'E2E Student', 'e2e@x.com') returning id"
                    ),
                    {
                        "d": uuid.UUID(drive_id),
                        "sid": STUDENT_ID,
                        "dob": date(2003, 5, 17),
                    },
                )
            ).scalar_one()
            await s.execute(
                text("insert into round_candidates (round_id, candidate_id) values (:r, :c)"),
                {"r": round_id, "c": cand_id},
            )
            await s.commit()
        round_id = str(round_id)

        q = await c.post(f"{API}/questions", json=question_body())
        assert q.status_code == 201, q.text
        qid = q.json()["id"]
        assert len(q.json()["test_cases"]) == 18

        t = await c.put(f"{API}/rounds/{round_id}/test", json={})
        assert t.status_code == 200, t.text
        test_id = t.json()["id"]
        assert t.json()["duration_sec"] == 300

        who["user"] = OTHER_HR  # another HR can neither see nor change this test
        assert (
            await c.put(f"{API}/tests/{test_id}/questions", json={"question_ids": [qid]})
        ).status_code == 404
        assert (await c.get(f"{API}/tests/{test_id}/results")).status_code == 404
        who["user"] = HR

        att = await c.put(f"{API}/tests/{test_id}/questions", json={"question_ids": [qid]})
        assert att.status_code == 200, att.text
        assert att.json()["levels"] == {"easy": 0, "medium": 1, "advanced": 0}

        # --- student side: real entry token and attempt token --------------------------------
        v = await c.post(f"{API}/entry/verify", json={"student_id": STUDENT_ID, "dob": DOB})
        assert v.status_code == 200, v.text
        entry = {"Authorization": f"Bearer {v.json()['entry_token']}"}
        assert v.json()["rounds"][0]["test_id"] == test_id  # HR's test row is the one used
        for step in ("location", "terms", "fullscreen"):
            s_ = await c.post(
                f"{API}/entry/steps",
                json={"round_id": round_id, "step": step, "ok": True},
                headers=entry,
            )
            assert s_.status_code == 204, s_.text
        st = await c.post(f"{API}/entry/start", json={"round_id": round_id}, headers=entry)
        assert st.status_code == 200, st.text
        attempt_id = st.json()["attempt_id"]
        me = {"Authorization": f"Bearer {st.json()['attempt_token']}"}
        assert (
            await c.post(f"{API}/entry/start", json={"round_id": round_id}, headers=entry)
        ).json()["attempt_id"] == attempt_id  # a reload gets the same attempt

        # GET /attempt: the coding question with its 3 samples, never a hidden case
        state = await c.get(f"{API}/attempt", headers=me)
        assert state.status_code == 200, state.text
        st_out = state.json()
        assert st_out["status"] == "in_progress"
        assert st_out["deadline_at"] and st_out["server_now"]
        coding = [x for x in st_out["questions"] if x["kind"] == "coding"]
        assert [x["id"] for x in coding] == [qid]
        assert coding[0]["marks"] == 10
        samples = [c_ for c_ in sum_cases() if c_.is_sample]
        assert coding[0]["samples"] == [
            {"input": c_.input, "expected": c_.expected} for c_ in samples
        ]
        assert "python" in {lang["key"] for lang in coding[0]["languages"]}
        for hidden in (c_ for c_ in sum_cases() if not c_.is_sample):
            assert hidden.input not in state.text  # inputs of hidden cases never reach a student
        assert st_out["saved_answers"] == {}

        # a code draft before any submit: recovered by GET /attempt/code-drafts only
        assert (await put_draft(c, me, qid, "draft v1")).status_code == 204
        assert await drafts(c, me) == {qid: "draft v1"}
        assert (await c.get(f"{API}/attempt", headers=me)).json()["saved_answers"] == {}

        run = await c.post(
            f"{API}/attempt/run",
            json={"question_id": qid, "code": SOLUTIONS["python"], "language": "python"},
            headers=me,
        )
        assert run.status_code == 200, run.text
        assert [x["status"] for x in run.json()["results"]] == ["passed"] * 3

        wrong = await c.post(
            f"{API}/attempt/submit-code",
            json={"question_id": qid, "code": "print(0)\n", "language": "python"},
            headers=me,
        )
        assert wrong.status_code == 200 and wrong.json()["passed"] < 18, wrong.text
        partial = (Decimal(10) * wrong.json()["passed"] / 18).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        )
        assert await answer_marks(sessions, attempt_id, qid) == partial  # marks x passed / total
        right = await c.post(
            f"{API}/attempt/submit-code",
            json={"question_id": qid, "code": SOLUTIONS["python"], "language": "python"},
            headers=me,
        )
        assert right.json() == {"passed": 18, "total": 18}, right.text  # latest submit wins
        assert await answer_marks(sessions, attempt_id, qid) == Decimal("10.00")
        saved = (await c.get(f"{API}/attempt", headers=me)).json()["saved_answers"]
        assert saved == {qid: {"code": SOLUTIONS["python"], "language": "python"}}
        assert await drafts(c, me) == {}  # the submission is newer than draft v1

        # a draft after the submit is newer, and leaves the submitted code and marks alone
        assert (await put_draft(c, me, qid, "draft v2")).status_code == 204
        assert await drafts(c, me) == {qid: "draft v2"}
        saved = (await c.get(f"{API}/attempt", headers=me)).json()["saved_answers"]
        assert saved == {qid: {"code": SOLUTIONS["python"], "language": "python"}}
        assert await answer_marks(sessions, attempt_id, qid) == Decimal("10.00")

        # --- back to HR: results and the lock ------------------------------------------------
        res = await c.get(f"{API}/tests/{test_id}/results")
        assert res.status_code == 200, res.text
        out = res.json()
        assert out["max_score"] == "10.00" and len(out["candidates"]) == 1
        cand = out["candidates"][0]
        assert (cand["student_id"], cand["status"], cand["score"]) == (
            STUDENT_ID,
            "in_progress",
            "10.00",
        )
        assert cand["answers"][0]["passed"] == 18 and cand["answers"][0]["submitted"] is True

        sub = await c.get(f"{API}/tests/{test_id}/results/{attempt_id}")
        assert sub.status_code == 200, sub.text
        assert sub.json()["submissions"][0]["code"] == SOLUTIONS["python"]
        assert "expected" not in res.text and "expected" not in sub.text  # no case text, ever

        # --- student's final submit: the attempt total includes the coding marks ----------
        fin = await c.post(f"{API}/attempt/submit", headers=me)
        assert fin.status_code == 200 and fin.json()["status"] == "submitted", fin.text
        async with sessions() as s:
            total = (
                await s.execute(
                    text("select marks from attempts where id = :a"), {"a": uuid.UUID(attempt_id)}
                )
            ).scalar_one()
        assert total == Decimal("10.00")
        after = (await c.get(f"{API}/tests/{test_id}/results")).json()["candidates"][0]
        assert (after["status"], after["score"]) == ("submitted", "10.00")  # same as the total
        for path in ("run", "submit-code"):
            late = await c.post(
                f"{API}/attempt/{path}",
                json={"question_id": qid, "code": "print(0)\n", "language": "python"},
                headers=me,
            )
            assert late.status_code == 409, late.text
            assert late.json()["error"]["code"] == "ATTEMPT_LOCKED"
        assert await answer_marks(sessions, attempt_id, qid) == Decimal("10.00")  # unchanged
        late_draft = await put_draft(c, me, qid, "too late")
        assert late_draft.status_code == 409, late_draft.text
        assert late_draft.json()["error"]["code"] == "ATTEMPT_LOCKED"
        assert await drafts(c, me) == {qid: "draft v2"}  # still readable after the submit

        locked = await c.put(f"{API}/tests/{test_id}/questions", json={"question_ids": []})
        assert locked.status_code == 409 and locked.json()["error"]["code"] == "TEST_IN_USE"
        again = await c.put(f"{API}/tests/{test_id}/questions", json={"question_ids": [qid]})
        assert again.status_code == 200  # the same list again is harmless

        async with sessions() as s:
            acts = (
                (
                    await s.execute(
                        text("select action from audit_logs where actor_id = :a order by id"),
                        {"a": HR.id},
                    )
                )
                .scalars()
                .all()
            )
        assert acts == [
            "drive.create",
            "question.create",
            "test.configure",
            "test.set_questions",
        ]

        # --- cleanup ----------------------------------------------------------------------
        async with sessions() as s:
            await s.execute(text("delete from drives where id = :d"), {"d": uuid.UUID(drive_id)})
            await s.execute(text("delete from questions where id = :q"), {"q": uuid.UUID(qid)})
            await s.commit()


async def _run() -> None:
    engine = create_async_engine(DB_URL, connect_args={"statement_cache_size": 0})
    try:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"E2E database is not reachable: {e}")
        async with async_sessionmaker(engine)() as s:
            for u in (HR, OTHER_HR):
                await s.execute(
                    text(
                        "insert into users (id, email, full_name, role) values (:i, :e, :n, 'hr')"
                    ),
                    {"i": u.id, "e": u.email, "n": u.full_name},
                )
            await s.commit()
        try:
            await _story(engine)
        finally:
            app.dependency_overrides.clear()
            async with async_sessionmaker(engine)() as s:
                await s.execute(
                    text("delete from audit_logs where actor_id in (:a, :b)"),
                    {"a": HR.id, "b": OTHER_HR.id},
                )
                await s.execute(
                    text("delete from drives where owner_id in (:a, :b)"),
                    {"a": HR.id, "b": OTHER_HR.id},
                )
                await s.execute(
                    text("delete from questions where created_by in (:a, :b)"),
                    {"a": HR.id, "b": OTHER_HR.id},
                )
                await s.execute(
                    text("delete from users where id in (:a, :b)"), {"a": HR.id, "b": OTHER_HR.id}
                )
                await s.commit()
    finally:
        await engine.dispose()


@pytest.mark.skipif(not DB_URL, reason="set E2E_DATABASE_URL to a scratch, migrated database")
def test_student_sits_a_coding_test_that_hr_set_up():
    try:
        r = httpx.get("http://localhost:2358/languages", timeout=3)
        up = r.status_code in (200, 401, 403)
    except httpx.HTTPError:
        up = False
    if not up:
        pytest.skip("Judge0 is not reachable: start it with `make judge0-up`")
    asyncio.run(_run())

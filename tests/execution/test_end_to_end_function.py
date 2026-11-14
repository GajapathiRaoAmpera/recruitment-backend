"""End to end for a FUNCTION-mode question: real Postgres (scratch DB) + live Judge0.

HR creates a function-mode question (signature stored as jsonb, cases canonicalised), renames
the function with PATCH (stored cases re-checked), attaches it to a test; the student gets
mode / signature / starter code / readable samples from GET /attempt (never the driver), runs
a Go solution built from the starter, is refused SQL, and submits a Java solution for 18/18.

INTEGRATION, skipped by default. Same scratch-database rule as test_end_to_end.py:

    E2E_DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:55432/e2e make test-judge0
"""

import asyncio
import os
import uuid
from datetime import date

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.main import app

pytestmark = pytest.mark.integration

DB_URL = os.environ.get("E2E_DATABASE_URL")
API = "/api/v1"
HR = StaffUser(
    id=uuid.uuid4(), email=f"e2e-{uuid.uuid4().hex[:8]}@x.com", full_name="E2E FN", role="hr"
)
STUDENT_ID = f"E2EF-{uuid.uuid4().hex[:8]}"

SIGNATURE = {
    "function_name": "countAbove",
    "parameters": [{"name": "nums", "type": "int[]"}, {"name": "limit", "type": "int"}],
    "return_type": "int",
}
ROWS = [([1, 5, 9], 4), ([], 0), ([3, 3, 3], 3)] + [
    (list(range(-i, i + 1)), i // 2) for i in range(15)
]
HIDDEN_MARKER = 777777


def cases() -> list[dict]:
    out = []
    for i, (nums, limit) in enumerate(ROWS):
        if i == 3:
            nums = [*nums, HIDDEN_MARKER]  # a hidden input that must never reach the student
        out.append(
            {
                "input": f"{nums}\n{limit}",  # HR's spacing: stored canonical
                "expected": str(sum(x > limit for x in nums)),
                "is_sample": i < 3,
            }
        )
    return out


GO_BODY = "\tc := 0\n\tfor _, x := range nums {\n\t\tif x > limit {\n\t\t\tc++\n\t\t}\n\t}\n"
JAVA = (
    "class Solution {\n    public int countAbove(int[] nums, int limit) {\n"
    "        int c = 0;\n        for (int x : nums) if (x > limit) c++;\n"
    '        System.out.println("debug");\n        return c;\n    }\n}\n'
)


async def _story(c: httpx.AsyncClient, sessions) -> None:
    r = await c.post(
        f"{API}/drives",
        json={
            "company": "E2E Fn Co",
            "campus": "Test Campus",
            "role_title": "Dev",
            "jd_text": "x",
            "rounds": [{"type": "shortlist"}, {"type": "coding", "duration_min": 5}],
        },
    )
    assert r.status_code == 201, r.text
    drive_id = uuid.UUID(r.json()["id"])
    async with sessions() as s:
        round_id = (
            await s.execute(
                text("select id from rounds where drive_id = :d and type = 'coding'"),
                {"d": drive_id},
            )
        ).scalar_one()
        cand = (
            await s.execute(
                text(
                    "insert into candidates (drive_id, student_id, dob, full_name, email) "
                    "values (:d, :sid, :dob, 'E2E Fn', 'e2ef@x.com') returning id"
                ),
                {"d": drive_id, "sid": STUDENT_ID, "dob": date(2003, 5, 17)},
            )
        ).scalar_one()
        await s.execute(
            text("insert into round_candidates (round_id, candidate_id) values (:r, :c)"),
            {"r": round_id, "c": cand},
        )
        await s.commit()

    # --- HR: create, rename via PATCH, attach --------------------------------------------
    q = await c.post(
        f"{API}/questions",
        json={
            "kind": "coding",
            "body": "Count Above\nReturn how many values in nums are greater than limit.",
            "difficulty": "easy",
            "marks": 10,
            "mode": "function",
            "signature": {**SIGNATURE, "function_name": "countBig"},
            "test_cases": cases(),
        },
    )
    assert q.status_code == 201, q.text
    qid = q.json()["id"]
    assert q.json()["mode"] == "function" and q.json()["test_cases"][0]["input"] == "[1,5,9]\n4\n"
    p = await c.patch(f"{API}/questions/{qid}", json={"signature": SIGNATURE})
    assert p.status_code == 200 and p.json()["signature"] == SIGNATURE, p.text
    got = await c.get(f"{API}/questions/{qid}")
    assert got.json()["signature"] == SIGNATURE and got.json()["mode"] == "function"
    async with sessions() as s:
        row = (
            await s.execute(
                text("select mode, signature from questions where id = :q"), {"q": uuid.UUID(qid)}
            )
        ).one()
        assert row.mode == "function"
    t = await c.put(f"{API}/rounds/{round_id}/test", json={})
    test_id = t.json()["id"]
    att = await c.put(f"{API}/tests/{test_id}/questions", json={"question_ids": [qid]})
    assert att.status_code == 200, att.text

    # --- student --------------------------------------------------------------------------
    v = await c.post(f"{API}/entry/verify", json={"student_id": STUDENT_ID, "dob": "2003-05-17"})
    entry = {"Authorization": f"Bearer {v.json()['entry_token']}"}
    for step in ("location", "terms", "fullscreen"):
        await c.post(
            f"{API}/entry/steps",
            json={"round_id": str(round_id), "step": step, "ok": True},
            headers=entry,
        )
    st = await c.post(f"{API}/entry/start", json={"round_id": str(round_id)}, headers=entry)
    assert st.status_code == 200, st.text
    me = {"Authorization": f"Bearer {st.json()['attempt_token']}"}

    state = await c.get(f"{API}/attempt", headers=me)
    assert state.status_code == 200, state.text
    [cq] = [x for x in state.json()["questions"] if x["kind"] == "coding"]
    assert cq["mode"] == "function" and cq["signature"] == SIGNATURE
    assert set(cq["starter_code"]) == {"python", "javascript", "c", "cpp", "java", "go"}
    assert "sql" not in {lang["key"] for lang in cq["languages"]}
    assert cq["samples"][0]["input_display"] == "nums = [1,5,9], limit = 4"
    assert cq["samples"][0]["expected"] == "2\n"
    for leak in (str(HIDDEN_MARKER), "_drv", "drvParse", "#line"):
        assert leak not in state.text

    go = cq["starter_code"]["go"].replace("{\n    \n}", "{\n" + GO_BODY + "\treturn c\n}")
    run = await c.post(
        f"{API}/attempt/run", json={"question_id": qid, "code": go, "language": "go"}, headers=me
    )
    assert run.status_code == 200, run.text
    assert [x["status"] for x in run.json()["results"]] == ["passed"] * 3, run.text
    assert "drv" not in run.text

    sql = await c.post(
        f"{API}/attempt/run",
        json={"question_id": qid, "code": "select 1;", "language": "sql"},
        headers=me,
    )
    assert sql.status_code == 422 and sql.json()["error"]["code"] == "LANGUAGE_NOT_ALLOWED"

    sub = await c.post(
        f"{API}/attempt/submit-code",
        json={"question_id": qid, "code": JAVA, "language": "java"},
        headers=me,
    )
    assert sub.status_code == 200 and sub.json() == {"passed": 18, "total": 18}, sub.text
    async with sessions() as s:
        saved = (
            await s.execute(
                text("select code, marks from answers where question_id = :q"),
                {"q": uuid.UUID(qid)},
            )
        ).one()
    assert saved.code == JAVA and float(saved.marks) == 10  # the student's code, no driver


async def _run() -> None:
    engine = create_async_engine(DB_URL, connect_args={"statement_cache_size": 0})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"E2E database is not reachable: {e}")
        async with sessions() as s:
            await s.execute(
                text("insert into users (id, email, full_name, role) values (:i, :e, :n, 'hr')"),
                {"i": HR.id, "e": HR.email, "n": HR.full_name},
            )
            await s.commit()

        async def get_session_override():
            async with sessions() as s:
                yield s

        app.dependency_overrides[get_session] = get_session_override
        app.dependency_overrides[security.current_user] = lambda: HR
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://e2e") as c:
                await _story(c, sessions)
        finally:
            app.dependency_overrides.clear()
            async with sessions() as s:
                for sql in (
                    "delete from audit_logs where actor_id = :a",
                    "delete from drives where owner_id = :a",
                    "delete from questions where created_by = :a",
                    "delete from users where id = :a",
                ):
                    await s.execute(text(sql), {"a": HR.id})
                await s.commit()
    finally:
        await engine.dispose()


@pytest.mark.skipif(not DB_URL, reason="set E2E_DATABASE_URL to a scratch, migrated database")
def test_student_solves_a_function_question_that_hr_set_up():
    try:
        up = httpx.get("http://localhost:2358/languages", timeout=3).status_code in (200, 401, 403)
    except httpx.HTTPError:
        up = False
    if not up:
        pytest.skip("Judge0 is not reachable: start it with `make judge0-up`")
    asyncio.run(_run())

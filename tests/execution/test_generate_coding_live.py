"""POST /questions/generate against real services. INTEGRATION tests, skipped by default; run
them with `make test-judge0`.

- test_live_nvidia_and_judge0_generate_one_question: one real NVIDIA call (NVIDIA_API_KEY) and a
  real Judge0 (`make judge0-up`). Skipped when either is missing. It spends one model request.
- test_generated_question_rows_in_a_real_database: the real SQL (approved = false,
  ai_generated, reference columns, audit row, "do not repeat" titles) on a scratch database.
  LLM and Judge0 are the unit-test fakes. Needs E2E_DATABASE_URL (a migrated scratch DB, never
  the .env database); removes what it created.
"""

import asyncio
import os
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import security
from app.auth.schemas import StaffUser
from app.core.config import get_settings
from app.core.db import get_session
from app.execution.coding_questions import generate as gen
from app.execution.coding_questions import repo
from app.execution.deps import judge0_client
from app.execution.judge0 import Judge0Client
from app.main import app
from tests.execution.test_generate_coding import FakeJudge0, FakeLlm, reply
from tests.execution.test_reference_solutions import judge0  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

DB_URL = os.environ.get("E2E_DATABASE_URL")


def test_live_nvidia_and_judge0_generate_one_question(judge0: Judge0Client):  # noqa: F811
    if not get_settings().nvidia_api_key:
        pytest.skip("NVIDIA_API_KEY is not set")
    drafts, failures = asyncio.run(
        gen.generate_many(
            gen.generation_llm(),
            judge0,
            difficulty="easy",
            topic="strings",
            count=1,
            avoid=[],
        )
    )
    assert drafts, f"no usable question: {[f.reason for f in failures]}"
    d = drafts[0]
    assert [c.is_sample for c in d.cases] == [True] * 3 + [False] * 15
    # expected came from Judge0: running the reference again gives the same output
    again = asyncio.run(judge0.run_many("python", d.question.reference_solution, d.question.inputs))
    assert [r.stdout for r in again] == [c.expected for c in d.cases]


async def _db_story() -> None:
    engine = create_async_engine(DB_URL, connect_args={"statement_cache_size": 0})
    hr = StaffUser(
        id=uuid.uuid4(), email=f"gen-{uuid.uuid4().hex[:8]}@x.com", full_name="Gen HR", role="hr"
    )
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
                {"i": hr.id, "e": hr.email, "n": hr.full_name},
            )
            await s.commit()

        async def session_override():
            async with sessions() as s:
                yield s

        title = f"Live DB {uuid.uuid4().hex[:6]}"
        llm = FakeLlm({0: [reply(title=title)]})
        fake_judge0 = FakeJudge0()
        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[security.current_user] = lambda: hr
        app.dependency_overrides[gen.generation_llm] = lambda: llm
        app.dependency_overrides[judge0_client] = lambda: fake_judge0
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as c:
                r = await c.post(
                    "/api/v1/questions/generate", json={"difficulty": "easy", "tags": ["live"]}
                )
                assert r.status_code == 201, r.text
                qid = r.json()["created"][0]["id"]
                got = (await c.get(f"/api/v1/questions/{qid}")).json()
                assert got["reference_solution"] and got["reference_language"] == "python"
        finally:
            app.dependency_overrides.clear()

        async with sessions() as s:
            row = (
                await s.execute(
                    text(
                        "select approved, ai_generated, reference_solution, reference_language, "
                        "(select count(*) from test_cases where question_id = q.id) as cases, "
                        "(select count(*) from audit_logs where entity_id = cast(q.id as text) "
                        "and action = 'question.generate') as audits "
                        "from questions q where id = :q"
                    ),
                    {"q": uuid.UUID(qid)},
                )
            ).one()
            assert row.approved is False and row.ai_generated is True
            assert row.reference_solution and row.reference_language == "python"
            assert (row.cases, row.audits) == (18, 1)
            assert title in await repo.recent_titles(s, hr.id, gen.MAX_AVOID)
            with pytest.raises(IntegrityError):  # the pair is set together or not at all
                await s.execute(
                    text("update questions set reference_language = null where id = :q"),
                    {"q": uuid.UUID(qid)},
                )
            await s.rollback()
    finally:
        async with sessions() as s:
            await s.execute(text("delete from audit_logs where actor_id = :a"), {"a": hr.id})
            await s.execute(text("delete from questions where created_by = :a"), {"a": hr.id})
            await s.execute(text("delete from users where id = :a"), {"a": hr.id})
            await s.commit()
        await engine.dispose()


@pytest.mark.skipif(not DB_URL, reason="set E2E_DATABASE_URL to a scratch, migrated database")
def test_generated_question_rows_in_a_real_database():
    asyncio.run(_db_story())

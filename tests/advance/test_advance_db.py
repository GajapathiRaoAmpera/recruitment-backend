"""Ranking + advance against a REAL Postgres: the repo SQL, the reused submit, and the flow
Round 1 submit -> advance -> student login -> /entry/start for Round 2.

INTEGRATION test, skipped by default (pyproject runs `-m 'not integration'`). It needs
`E2E_DATABASE_URL` pointing at a scratch, migrated database; it never touches the .env database
and removes every row it creates. No Judge0 is needed.

    E2E_DATABASE_URL=postgresql+asyncpg://postgres:pw@127.0.0.1:55432/e2e \
        uv run pytest -m integration tests/advance -q

Only the HR login is stubbed; students use the real entry and attempt tokens.
"""

import asyncio
import json
import os
import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import entry as auth_entry
from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.main import app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("E2E_DATABASE_URL"),
        reason="set E2E_DATABASE_URL to a scratch, migrated database",
    ),
]

DB_URL = os.environ.get("E2E_DATABASE_URL", "")
API = "/api/v1"
DOB = date(2003, 5, 17)


class World:
    """One test's drive, people and helpers. Every row hangs off the two HR users."""

    def __init__(self, engine, client):
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.c = client
        self.hr = StaffUser(
            id=uuid.uuid4(), email=f"adv-{uuid.uuid4().hex[:8]}@x.com", full_name="HR", role="hr"
        )
        self.other = StaffUser(
            id=uuid.uuid4(), email=f"adv-{uuid.uuid4().hex[:8]}@x.com", full_name="HR2", role="hr"
        )
        self.who = {"user": self.hr}

    async def sql(self, query: str, params: dict | None = None):
        async with self.sessions() as s:
            result = await s.execute(text(query), params or {})
            rows = list(result.mappings()) if result.returns_rows else []
            await s.commit()
            return rows

    async def drive(self, rounds: list[dict]) -> list[uuid.UUID]:
        """Create through the real route; returns round ids by position."""
        r = await self.c.post(
            f"{API}/drives",
            json={"company": "Adv Co", "campus": "C", "role_title": "Dev", "jd_text": "x",
                  "rounds": rounds},
        )  # fmt: skip
        assert r.status_code == 201, r.text
        self.drive_id = uuid.UUID(r.json()["id"])
        rows = await self.sql(
            "select id from rounds where drive_id = :d order by position", {"d": self.drive_id}
        )
        return [row["id"] for row in rows]

    async def student(self, name: str, *round_ids) -> uuid.UUID:
        sid = f"ADV-{name}-{uuid.uuid4().hex[:6]}"
        (row,) = await self.sql(
            "insert into candidates (drive_id, student_id, dob, full_name, email) "
            "values (:d, :sid, :dob, :n, 'a@x.com') returning id",
            {"d": self.drive_id, "sid": sid, "dob": DOB, "n": name},
        )
        for rid in round_ids:
            await self.sql(
                "insert into round_candidates (round_id, candidate_id) values (:r, :c)",
                {"r": rid, "c": row["id"]},
            )
        self.sids = getattr(self, "sids", {}) | {row["id"]: sid}
        return row["id"]

    async def test(self, round_id) -> uuid.UUID:
        (row,) = await self.sql(
            "insert into tests (round_id, duration_sec, question_count) values (:r, 1800, 0) "
            "returning id",
            {"r": round_id},
        )
        return row["id"]

    async def mcq(self, marks) -> uuid.UUID:
        """Option 1 is right."""
        (row,) = await self.sql(
            "insert into questions (kind, body, options, answer, difficulty, marks, approved, "
            "created_by) values ('mcq', 'Q?', cast(:o as jsonb), cast('1' as jsonb), 'easy', :m, "
            "true, :u) returning id",
            {"o": json.dumps(["a", "b", "c", "d"]), "m": marks, "u": self.hr.id},
        )
        return row["id"]

    async def coding_on_test(self, test_id, marks) -> uuid.UUID:
        (row,) = await self.sql(
            "insert into questions (kind, body, difficulty, marks, approved, created_by) "
            "values ('coding', 'Sum', 'easy', :m, true, :u) returning id",
            {"m": marks, "u": self.hr.id},
        )
        await self.sql(
            "insert into test_questions (test_id, question_id, position) values (:t, :q, 0)",
            {"t": test_id, "q": row["id"]},
        )
        return row["id"]

    async def ready_coding(self, round_id) -> None:
        """Advancing into a coding round (and starting it) needs its questions set up."""
        await self.coding_on_test(await self.test(round_id), 10)

    async def attempt(
        self, test_id, cid, status="submitted", *, marks=None, secs=None, paper=(),
        answers=None, deadline_min=30,
    ) -> uuid.UUID:  # fmt: skip
        """paper: MCQ ids on this student's own paper; answers: {qid: shown option}."""
        (row,) = await self.sql(
            "insert into attempts (test_id, candidate_id, status, started_at, deadline_at, "
            "submitted_at, marks, time_taken_sec) values (:t, :c, cast(:s as attempt_status), "
            "now() - interval '10 minutes', now() + make_interval(mins => :dl), "
            "case when :s = 'submitted' then now() end, :m, :secs) returning id",
            {"t": test_id, "c": cid, "s": status, "m": marks, "secs": secs, "dl": deadline_min},
        )
        for n, q in enumerate(paper):
            await self.sql(
                "insert into attempt_questions (attempt_id, question_id, position, option_order) "
                "values (:a, :q, :p, '{0,1,2,3}')",
                {"a": row["id"], "q": q, "p": n},
            )
        for q, resp in (answers or {}).items():
            await self.sql(
                "insert into answers (attempt_id, question_id, response) "
                "values (:a, :q, cast(:r as jsonb))",
                {"a": row["id"], "q": q, "r": json.dumps(resp)},
            )
        return row["id"]

    async def rc(self, round_id) -> dict:
        rows = await self.sql(
            "select candidate_id, cast(status as text) as status, score, rank "
            "from round_candidates where round_id = :r",
            {"r": round_id},
        )
        return {r["candidate_id"]: r for r in rows}

    async def round_status(self, round_id) -> str:
        (row,) = await self.sql(
            "select cast(status as text) as s, threshold from rounds where id = :r", {"r": round_id}
        )
        return row["s"]

    async def ranking(self, round_id, threshold=None) -> dict:
        q = "" if threshold is None else f"?threshold={threshold}"
        r = await self.c.get(f"{API}/rounds/{round_id}/ranking{q}")
        assert r.status_code == 200, r.text
        return r.json()

    async def advance(self, round_id, threshold, **extra) -> httpx.Response:
        return await self.c.post(
            f"{API}/rounds/{round_id}/advance",
            json={"threshold": threshold, "mark_absent": True, **extra},
        )


def run(story) -> None:
    async def main():
        engine = create_async_engine(DB_URL, connect_args={"statement_cache_size": 0})
        try:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("select 1"))
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"E2E database is not reachable: {e}")
            sessions = async_sessionmaker(engine, expire_on_commit=False)

            async def session_override():
                async with sessions() as s:
                    yield s

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://adv") as c:
                w = World(engine, c)
                app.dependency_overrides[get_session] = session_override
                app.dependency_overrides[security.current_user] = lambda: w.who["user"]
                ids = {"a": w.hr.id, "b": w.other.id}
                for u in (w.hr, w.other):
                    await w.sql(
                        "insert into users (id, email, full_name, role) values (:i, :e, :n, 'hr')",
                        {"i": u.id, "e": u.email, "n": u.full_name},
                    )
                try:
                    await story(w)
                finally:
                    app.dependency_overrides.clear()
                    for q in (
                        "delete from audit_logs where actor_id in (:a, :b)",
                        "delete from drives where owner_id in (:a, :b)",  # cascades the rest
                        "delete from questions where created_by in (:a, :b)",
                        "delete from users where id in (:a, :b)",
                    ):
                        await w.sql(q, ids)
        finally:
            await engine.dispose()

    asyncio.run(main())


STANDARD = [
    {"type": "shortlist"},
    {"type": "aptitude", "threshold": 60, "duration_min": 30},
    {"type": "coding", "threshold": 50, "duration_min": 30},
    {"type": "final", "mode": "manual"},
]


# ---- ranking math on real rows ---------------------------------------------------------------


def test_ranking_uses_each_students_own_max_marks_and_breaks_ties_on_time():
    async def story(w: World):
        _, apt, *_ = await w.drive(STANDARD)
        t = await w.test(apt)
        q2a, q2b, q1 = await w.mcq(2), await w.mcq(2), await w.mcq(1)
        coding = await w.coding_on_test(t, 10)  # same for everyone: +10 to every max
        assert coding
        asha, ben, cara, dev = [await w.student(n, apt) for n in ("Asha", "Ben", "Cara", "Dev")]
        # Asha 7 of 14 = 50 %, Ben 11 of 11 = 100 %, Cara 5.5 of 11 = 50 % but faster than Asha
        await w.attempt(t, asha, marks=7, secs=600, paper=[q2a, q2b])
        await w.attempt(t, ben, marks=11, secs=900, paper=[q1])
        await w.attempt(t, cara, marks=Decimal("5.5"), secs=300, paper=[q1])
        out = await w.ranking(apt, 50)
        got = [(c["name"], c["max_marks"], c["percent"], c["rank"], c["would_pass"])
               for c in out["candidates"]]  # fmt: skip
        assert got == [
            ("Ben", 11.0, 100.0, 1, True),
            ("Cara", 11.0, 50.0, 2, True),  # 50 == 50 passes
            ("Asha", 14.0, 50.0, 3, True),
            ("Dev", None, None, None, False),  # never started
        ]
        assert out["threshold"] == 50.0 and out["round_status"] == "active"
        assert out["counts"] == {"pass": 3, "fail": 0, "absent": 1, "still_writing": 0,
                                 "locked": 0}  # fmt: skip
        saved = await w.ranking(apt)  # no query: the round's saved 60
        assert saved["threshold"] == 60.0 and saved["counts"]["pass"] == 1
        assert dev in (await w.rc(apt))  # still invited: the preview wrote nothing

    run(story)


def test_other_hr_gets_404_and_super_admin_sees_it():
    async def story(w: World):
        _, apt, *_ = await w.drive(STANDARD)
        w.who["user"] = w.other
        for r in (
            await w.c.get(f"{API}/rounds/{apt}/ranking"),
            await w.advance(apt, 50),
        ):
            assert r.status_code == 404 and r.json()["error"]["code"] == "ROUND_NOT_FOUND"
        assert await w.round_status(apt) == "active"
        w.who["user"] = w.other.model_copy(update={"role": "super_admin"})
        assert (await w.c.get(f"{API}/rounds/{apt}/ranking")).status_code == 200

    run(story)


# ---- advance ---------------------------------------------------------------------------------


def test_advance_saves_results_opens_next_round_and_audits():
    async def story(w: World):
        _, apt, cod, fin = await w.drive(STANDARD)
        await w.ready_coding(cod)
        t = await w.test(apt)
        q = await w.mcq(10)
        top, edge, low = [await w.student(n, apt) for n in ("Top", "Edge", "Low")]
        gone = await w.student("Gone", apt)
        await w.attempt(t, top, marks=9, secs=100, paper=[q])
        await w.attempt(t, edge, marks=6, secs=100, paper=[q])  # exactly 60 %
        await w.attempt(t, low, marks=Decimal("5.99"), secs=50, paper=[q])
        r = await w.advance(apt, 60)
        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["passed"], out["failed"], out["absent"]) == (2, 1, 1)
        assert out["next_round_id"] == str(cod) and out["threshold"] == 60.0
        assert {i["candidate_id"] for i in out["invited"]} == {str(top), str(edge)}

        rc = await w.rc(apt)
        assert {k: (v["status"], v["score"], v["rank"]) for k, v in rc.items()} == {
            top: ("passed", Decimal("90.00"), 1),
            edge: ("passed", Decimal("60.00"), 2),
            low: ("failed", Decimal("59.90"), 3),
            gone: ("absent", None, None),
        }
        assert await w.round_status(apt) == "completed"
        assert await w.round_status(cod) == "active" and await w.round_status(fin) == "pending"
        nxt = await w.rc(cod)
        assert set(nxt) == {top, edge} and {v["status"] for v in nxt.values()} == {"invited"}
        (a,) = await w.sql(
            "select actor_id, entity, entity_id, diff from audit_logs "
            "where action = 'round.advance' and entity_id = :r",
            {"r": str(apt)},
        )
        assert a["actor_id"] == w.hr.id and a["entity"] == "round"
        assert a["diff"]["passed"] == 2 and a["diff"]["next_round_id"] == str(cod)
        assert sorted(a["diff"]["invited"]) == sorted([str(top), str(edge)])

        again = await w.advance(apt, 60)
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "ROUND_ALREADY_ADVANCED"
        n = await w.sql(
            "select count(*) as n from audit_logs where action = 'round.advance' "
            "and entity_id = :r",
            {"r": str(apt)},
        )
        assert n[0]["n"] == 1

    run(story)


def test_still_writing_refuses_and_saves_nothing():
    async def story(w: World):
        _, apt, cod, _ = await w.drive(STANDARD)
        await w.ready_coding(cod)
        t = await w.test(apt)
        q = await w.mcq(1)
        done, busy = await w.student("Done", apt), await w.student("Busy", apt)
        await w.attempt(t, done, marks=1, secs=10, paper=[q])
        await w.attempt(t, busy, "in_progress", paper=[q])  # deadline 30 min away
        r = await w.advance(apt, 50)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "ROUND_STILL_RUNNING"
        assert err["details"]["counts"]["still_writing"] == 1
        assert await w.round_status(apt) == "active" and await w.round_status(cod) == "pending"
        assert {v["status"] for v in (await w.rc(apt)).values()} == {"invited"}
        assert await w.rc(cod) == {}

    run(story)


def test_past_deadline_attempt_is_finalized_by_the_real_submit():
    async def story(w: World):
        _, apt, cod, _ = await w.drive(STANDARD)
        await w.ready_coding(cod)
        t = await w.test(apt)
        q1, q2 = await w.mcq(5), await w.mcq(5)
        late = await w.student("Late", apt)
        # in progress, deadline passed 5 min ago, one right (1) and one wrong (3) answer saved
        aid = await w.attempt(
            t, late, "in_progress", paper=[q1, q2], answers={q1: 1, q2: 3}, deadline_min=-5
        )
        preview = await w.ranking(apt, 50)  # the preview finalizes it too (same as GET /attempt)
        (c,) = preview["candidates"]
        assert (c["attempt_status"], c["marks"], c["percent"]) == ("submitted", 5.0, 50.0)
        assert preview["counts"]["still_writing"] == 0
        (att,) = await w.sql(
            "select cast(status as text) as s, marks, time_taken_sec from attempts where id = :a",
            {"a": aid},
        )
        assert att["s"] == "submitted" and att["marks"] == Decimal("5.00")
        assert 290 <= att["time_taken_sec"] <= 310  # capped at the deadline: 10 min - 5 min

        aid2 = await w.attempt(
            t, await w.student("Late2", apt), "in_progress", paper=[q1], answers={q1: 1},
            deadline_min=-1,
        )  # fmt: skip
        out = (await w.advance(apt, 50)).json()
        assert (out["passed"], out["failed"]) == (2, 0)
        (a,) = await w.sql(
            "select diff from audit_logs where action = 'round.advance' and entity_id = :r",
            {"r": str(apt)},
        )
        assert a["diff"]["finalized_expired"] == [str(aid2)]

    run(story)


def test_locked_attempt_refuses_until_hr_finalizes_it():
    async def story(w: World):
        _, apt, cod, _ = await w.drive(STANDARD)
        await w.ready_coding(cod)
        t = await w.test(apt)
        q = await w.mcq(4)
        lock = await w.student("Lock", apt)
        aid = await w.attempt(t, lock, "locked", paper=[q], answers={q: 1})
        r = await w.advance(apt, 50)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "LOCKED_ATTEMPTS"
        assert err["details"]["attempts"][0]["attempt_id"] == str(aid)
        assert await w.round_status(apt) == "active"
        (att,) = await w.sql("select cast(status as text) as s from attempts where id = :a",
                             {"a": aid})  # fmt: skip
        assert att["s"] == "locked"  # nothing changed

        r = await w.advance(apt, 50, finalize_locked=True)
        assert r.status_code == 200, r.text
        assert r.json()["passed"] == 1
        (att,) = await w.sql(
            "select cast(status as text) as s, marks from attempts where id = :a", {"a": aid}
        )
        assert att["s"] == "submitted" and att["marks"] == Decimal("4.00")  # scored by submit
        assert (await w.rc(apt))[lock]["status"] == "passed"

    run(story)


def test_skipped_next_round_is_jumped_over():
    async def story(w: World):
        _, apt, cod, tech, fin = await w.drive(
            [{"type": "shortlist"}, {"type": "aptitude"}, {"type": "coding"},
             {"type": "technical"}, {"type": "final", "mode": "manual"}]
        )  # fmt: skip
        await w.sql("update rounds set status = 'skipped' where id = :r", {"r": cod})
        t = await w.test(apt)
        s = await w.student("S", apt)
        await w.attempt(t, s, marks=1, secs=1, paper=[await w.mcq(1)])
        out = (await w.advance(apt, 50)).json()
        assert out["next_round_id"] == str(tech)
        assert await w.round_status(cod) == "skipped" and await w.round_status(tech) == "active"
        assert set(await w.rc(tech)) == {s} and await w.rc(cod) == {}

    run(story)


def test_last_round_just_finishes():
    async def story(w: World):
        _, apt, cod = await w.drive(
            [{"type": "shortlist"}, {"type": "aptitude"}, {"type": "coding"}]
        )
        await w.sql("update rounds set status = 'completed' where id = :r", {"r": apt})
        await w.sql("update rounds set status = 'active' where id = :r", {"r": cod})
        t = await w.test(cod)
        s = await w.student("S", cod)
        await w.attempt(t, s, marks=3, secs=1)  # coding: max comes from test_questions
        await w.coding_on_test(t, 4)
        out = (await w.advance(cod, 70)).json()
        assert out["next_round_id"] is None and out["invited"] == []
        assert out["passed"] == 1  # 3 of 4 = 75 %
        assert await w.round_status(cod) == "completed"

    run(story)


def test_pending_round_is_refused():
    async def story(w: World):
        _, _, cod, _ = await w.drive(STANDARD)
        r = await w.advance(cod, 50)
        assert r.status_code == 409 and r.json()["error"]["code"] == "ROUND_NOT_ACTIVE"

    run(story)


# ---- the whole flow --------------------------------------------------------------------------


def test_round1_submit_advance_login_and_start_round2():
    async def story(w: World):
        _, apt, cod, _ = await w.drive(STANDARD)
        await w.ready_coding(cod)
        t = await w.test(apt)
        q1, q2 = await w.mcq(1), await w.mcq(1)
        good, bad = await w.student("Good", apt), await w.student("Bad", apt)
        tokens = {}
        for cid in (good, bad):
            aid = await w.attempt(t, cid, "in_progress", paper=[q1, q2])
            tokens[cid] = {"Authorization": f"Bearer {auth_entry.issue_attempt_token(aid, cid)}"}
        # Round 1 answers through the real routes, then the real final submit
        for q in (q1, q2):
            r = await w.c.put(f"{API}/attempt/answers/{q}", json={"response": 1},
                              headers=tokens[good])  # fmt: skip
            assert r.status_code == 204, r.text
            r = await w.c.put(f"{API}/attempt/answers/{q}", json={"response": 0},
                              headers=tokens[bad])  # fmt: skip
            assert r.status_code == 204, r.text
        for cid in (good, bad):
            r = await w.c.post(f"{API}/attempt/submit", headers=tokens[cid])
            assert r.status_code == 200 and r.json()["status"] == "submitted", r.text

        out = (await w.advance(apt, 50)).json()
        assert (out["passed"], out["failed"]) == (1, 1)
        assert [i["candidate_id"] for i in out["invited"]] == [str(good)]

        # failed student: nothing open any more (Round 1 done, not in Round 2)
        r = await w.c.post(f"{API}/entry/verify",
                           json={"student_id": w.sids[bad], "dob": DOB.isoformat()})  # fmt: skip
        rounds = r.json().get("rounds", []) if r.status_code == 200 else []
        assert r.status_code in (200, 401) and str(cod) not in [x["round_id"] for x in rounds]

        # passed student: Round 2 is listed and can be started
        r = await w.c.post(f"{API}/entry/verify",
                           json={"student_id": w.sids[good], "dob": DOB.isoformat()})  # fmt: skip
        assert r.status_code == 200, r.text
        assert [x["round_id"] for x in r.json()["rounds"]] == [str(cod)]
        entry = {"Authorization": f"Bearer {r.json()['entry_token']}"}
        for step in ("location", "terms", "fullscreen"):
            r = await w.c.post(f"{API}/entry/steps",
                               json={"round_id": str(cod), "step": step, "ok": True},
                               headers=entry)  # fmt: skip
            assert r.status_code == 204, r.text
        r = await w.c.post(f"{API}/entry/start", json={"round_id": str(cod)}, headers=entry)
        assert r.status_code == 200, r.text
        (att,) = await w.sql(
            "select a.candidate_id, cast(a.status as text) as s, t.round_id from attempts a "
            "join tests t on t.id = a.test_id where a.id = :a",
            {"a": uuid.UUID(r.json()["attempt_id"])},
        )
        assert (att["candidate_id"], att["s"], att["round_id"]) == (good, "in_progress", cod)
        assert (await w.rc(cod))[good]["status"] == "in_progress"

    run(story)

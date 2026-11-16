"""HR resume, student sign-in and /entry/start against a REAL Postgres (the SQL the API tests fake).

INTEGRATION test, skipped by default. Needs `E2E_DATABASE_URL` pointing at a scratch, migrated
database (never the .env one); every row it creates is removed. No Judge0, no AI.

    E2E_DATABASE_URL=postgresql+asyncpg://postgres:pw@127.0.0.1:55432/e2e \
        uv run pytest -m integration tests/integrity -q

Same harness as tests/advance/test_advance_db.py: only the HR login is stubbed; students use
the real entry and attempt tokens.
"""

import uuid
from datetime import UTC, datetime

from app.auth import entry as auth_entry
from tests.advance.test_advance_db import API, DOB, STANDARD, World, pytestmark, run

__all__ = ["pytestmark"]  # same integration marker + skip rule


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _att(w: World, aid) -> dict:
    (row,) = await w.sql(
        "select cast(status as text) as status, tab_switches, remaining_sec_on_lock, "
        "resume_count, marks, extract(epoch from deadline_at - now()) as left_sec "
        "from attempts where id = :a",
        {"a": aid},
    )
    return dict(row)


async def _locked(w: World, *, remaining=300, overdue_min=60):
    """A drive with an aptitude round, one student whose attempt the 3rd tab switch locked
    (through the real route). Then, as if the student stayed locked for a long time, the old
    deadline is pushed into the past and the saved time left set to `remaining`."""
    _, apt, cod, _ = await w.drive(STANDARD)
    t = await w.test(apt)
    q = await w.mcq(2)
    cid = await w.student("Lock", apt)
    await w.sql("update round_candidates set status = 'in_progress' where candidate_id = :c",
                {"c": cid})  # fmt: skip
    aid = await w.attempt(t, cid, "in_progress", paper=[q], answers={q: 1})
    tok = _h(auth_entry.issue_attempt_token(aid, cid))
    for n in (1, 2, 3):
        r = await w.c.post(f"{API}/attempt/violations", json={"kind": "tab_switch"}, headers=tok)
        assert r.json() == {"tab_switches": n, "locked": n == 3}, r.text
    await w.sql(
        "update attempts set deadline_at = now() - make_interval(mins => :m), "
        "remaining_sec_on_lock = :s where id = :a",
        {"a": aid, "s": remaining, "m": overdue_min},
    )
    return apt, cod, t, q, cid, aid, tok


async def _resume(w: World, aid, reason="Power cut in lab 2"):
    return await w.c.post(f"{API}/attempts/{aid}/resume", json={"reason": reason})


async def _sign_in(w: World, cid, dob=DOB):
    return await w.c.post(
        f"{API}/entry/verify", json={"student_id": w.sids[cid], "dob": dob.isoformat()}
    )


async def _system_check(w: World, entry_h, round_id):
    for step in ("location", "terms", "fullscreen"):
        r = await w.c.post(f"{API}/entry/steps", json={"round_id": str(round_id), "step": step,
                                                       "ok": True}, headers=entry_h)  # fmt: skip
        assert r.status_code == 204, r.text


# ---- B1: resume ------------------------------------------------------------------------------


def test_resume_restores_the_time_left_resets_the_counter_keeps_history_and_audits():
    async def story(w: World):
        apt, _, _, q, cid, aid, _ = await _locked(w, remaining=300, overdue_min=60)
        before = await _att(w, aid)
        assert before["status"] == "locked" and before["tab_switches"] == 3
        assert before["left_sec"] < -3500  # the old deadline passed an hour ago

        r = await _resume(w, aid, "  Power cut in lab 2  ")
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["attempt_id"] == str(aid) and out["status"] == "in_progress"
        assert out["tab_switches"] == 0
        left = (datetime.fromisoformat(out["deadline_at"]) - datetime.now(UTC)).total_seconds()
        assert 290 <= left <= 301  # now + the 300 s left at lock, not the old deadline

        after = await _att(w, aid)
        assert (after["status"], after["tab_switches"], after["remaining_sec_on_lock"]) == (
            "in_progress", 0, None)  # fmt: skip
        assert after["resume_count"] == 1 and after["marks"] is None
        assert 290 <= after["left_sec"] <= 301
        (v,) = await w.sql("select count(*) as n from violations where attempt_id = :a",
                           {"a": aid})  # fmt: skip
        assert v["n"] == 3  # history is kept
        (ans,) = await w.sql("select response from answers where attempt_id = :a", {"a": aid})
        assert ans["response"] == 1  # answers untouched

        (a,) = await w.sql(
            "select actor_id, entity, diff from audit_logs where action = 'attempt.resume' "
            "and entity_id = :a",
            {"a": str(aid)},
        )
        assert a["actor_id"] == w.hr.id and a["entity"] == "attempt"
        assert a["diff"]["reason"] == "Power cut in lab 2"
        assert a["diff"]["previous_tab_switches"] == 3 and a["diff"]["restored_sec"] == 300

        # B2: HR's progress keeps the historical total and shows the resume
        r = await w.c.get(f"{API}/drives/{w.drive_id}/rounds/{apt}/progress")
        (row,) = r.json()["candidates"]
        assert row["attempt_id"] == str(aid) and row["resume_count"] == 1
        assert row["tab_switches"] == 3 and row["attempt_status"] == "in_progress"

        again = await _resume(w, aid)
        assert again.status_code == 409 and again.json()["error"]["code"] == "NOT_LOCKED"
        (n,) = await w.sql("select count(*) as n from audit_logs where action = 'attempt.resume' "
                           "and entity_id = :a", {"a": str(aid)})  # fmt: skip
        assert n["n"] == 1

    run(story)


def test_other_hr_gets_404_and_super_admin_can_resume():
    async def story(w: World):
        *_, aid, _ = await _locked(w)
        w.who["user"] = w.other
        r = await _resume(w, aid)
        assert r.status_code == 404 and r.json()["error"]["code"] == "ATTEMPT_NOT_FOUND"
        assert (await _att(w, aid))["status"] == "locked"
        r = await w.c.post(f"{API}/attempts/{uuid.uuid4()}/resume", json={"reason": "abc"})
        assert r.status_code == 404
        w.who["user"] = w.other.model_copy(update={"role": "super_admin"})
        assert (await _resume(w, aid)).status_code == 200

    run(story)


def test_round_not_active_and_unknown_time_are_refused():
    async def story(w: World):
        apt, *_, aid, _ = await _locked(w)
        await w.sql("update rounds set status = 'completed' where id = :r", {"r": apt})
        r = await _resume(w, aid)
        assert r.status_code == 409 and r.json()["error"]["code"] == "ROUND_NOT_ACTIVE"
        await w.sql("update rounds set status = 'active' where id = :r", {"r": apt})
        await w.sql("update attempts set remaining_sec_on_lock = null where id = :a", {"a": aid})
        r = await _resume(w, aid)
        assert r.status_code == 409 and r.json()["error"]["code"] == "REMAINING_TIME_UNKNOWN"
        assert (await _att(w, aid))["status"] == "locked"
        (n,) = await w.sql("select count(*) as n from audit_logs where action = 'attempt.resume' "
                           "and entity_id = :a", {"a": str(aid)})  # fmt: skip
        assert n["n"] == 0

    run(story)


def test_resumed_student_signs_in_starts_the_same_attempt_and_the_third_switch_locks_again():
    async def story(w: World):
        apt, _, _, _, cid, aid, _ = await _locked(w, remaining=900)
        # locked: listed with its status, /entry/start says paused
        r = await _sign_in(w, cid)
        assert r.status_code == 200, r.text
        assert [(x["round_id"], x["attempt_status"]) for x in r.json()["rounds"]] == [
            (str(apt), "locked")
        ]
        entry_h = _h(r.json()["entry_token"])
        await _system_check(w, entry_h, apt)
        r = await w.c.post(f"{API}/entry/start", json={"round_id": str(apt)}, headers=entry_h)
        assert r.status_code == 409 and r.json()["error"]["code"] == "ATTEMPT_LOCKED"

        assert (await _resume(w, aid)).status_code == 200
        rounds = await w.c.get(f"{API}/entry/rounds", headers=entry_h)
        assert rounds.json()["rounds"][0]["attempt_status"] == "in_progress"
        r = await w.c.post(f"{API}/entry/start", json={"round_id": str(apt)}, headers=entry_h)
        assert r.status_code == 200, r.text
        assert r.json()["attempt_id"] == str(aid)  # the same attempt
        tok = _h(r.json()["attempt_token"])
        state = (await w.c.get(f"{API}/attempt", headers=tok)).json()
        assert state["status"] == "in_progress" and state["tab_switches"] == 0

        for n in (1, 2, 3):  # the 3-switch rule is unchanged after a resume
            r = await w.c.post(f"{API}/attempt/violations", json={"kind": "tab_switch"},
                               headers=tok)  # fmt: skip
            assert r.json() == {"tab_switches": n, "locked": n == 3}
        after = await _att(w, aid)
        assert after["status"] == "locked" and 880 <= after["remaining_sec_on_lock"] <= 900
        r = await w.c.get(f"{API}/drives/{w.drive_id}/rounds/{apt}/progress")
        assert r.json()["candidates"][0]["tab_switches"] == 6  # 3 before + 3 after the resume

    run(story)


# ---- B5 / B6 / B7: sign-in and start for finished students -----------------------------------


def test_submitted_student_signs_in_sees_the_round_and_start_says_submitted():
    async def story(w: World):
        _, apt, *_ = await w.drive(STANDARD)
        t = await w.test(apt)
        cid = await w.student("Done", apt)
        await w.attempt(t, cid, "submitted", marks=1, secs=60)
        r = await _sign_in(w, cid)
        assert r.status_code == 200, r.text  # not "ID or DOB does not match"
        assert [(x["round_id"], x["attempt_status"]) for x in r.json()["rounds"]] == [
            (str(apt), "submitted")
        ]
        logs = await w.sql("select count(*) as n from verification_logs where candidate_id = :c",
                           {"c": cid})  # fmt: skip
        assert logs[0]["n"] == 0  # a submitted round is not sittable: no id_dob logged
        entry_h = _h(r.json()["entry_token"])
        same = await w.c.get(f"{API}/entry/rounds", headers=entry_h)
        assert same.json() == {"rounds": r.json()["rounds"]}
        r = await w.c.post(f"{API}/entry/start", json={"round_id": str(apt)}, headers=entry_h)
        assert r.status_code == 409 and r.json()["error"]["code"] == "ATTEMPT_SUBMITTED"

    run(story)


def test_wrong_dob_of_a_finished_student_counts_toward_the_15_minute_lockout():
    async def story(w: World):
        _, apt, *_ = await w.drive(STANDARD)
        t = await w.test(apt)
        cid = await w.student("Done", apt)
        await w.attempt(t, cid, "submitted", marks=1, secs=60)
        wrong = DOB.replace(day=18)
        for _ in range(5):
            r = await _sign_in(w, cid, wrong)
            assert r.status_code == 401 and r.json()["error"]["code"] == "ENTRY_MISMATCH"
        rows = await w.sql("select round_id, ok from verification_logs where candidate_id = :c",
                           {"c": cid})  # fmt: skip
        assert [(x["round_id"], x["ok"]) for x in rows] == [(apt, False)] * 5
        r = await _sign_in(w, cid)  # even the right DOB is refused now
        assert r.status_code == 429 and r.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"
        unknown = await w.c.post(f"{API}/entry/verify",
                                 json={"student_id": "NOPE-" + uuid.uuid4().hex[:6],
                                       "dob": DOB.isoformat()})  # fmt: skip
        assert unknown.status_code == 401 and unknown.json()["error"]["code"] == "ENTRY_MISMATCH"

    run(story)


def test_advanced_student_signs_in_with_no_rounds_and_misses_log_against_the_latest_round():
    async def story(w: World):
        _, apt, cod, _ = await w.drive(STANDARD)
        cid = await w.student("Out", apt)
        await w.sql("update round_candidates set status = 'failed' where candidate_id = :c",
                    {"c": cid})  # fmt: skip
        await w.sql("update rounds set status = 'completed' where id = :r", {"r": apt})
        r = await _sign_in(w, cid)
        assert r.status_code == 200 and r.json()["rounds"] == []  # completed rounds stay hidden
        r = await _sign_in(w, cid, DOB.replace(day=18))
        assert r.status_code == 401
        (log,) = await w.sql("select round_id, ok from verification_logs where candidate_id = :c",
                             {"c": cid})  # fmt: skip
        assert (log["round_id"], log["ok"]) == (apt, False)

    run(story)


def test_past_deadline_reads_expired_and_start_finalizes_it_with_the_real_submit():
    async def story(w: World):
        _, apt, *_ = await w.drive(STANDARD)
        t = await w.test(apt)
        q1, q2 = await w.mcq(5), await w.mcq(5)
        cid = await w.student("Late", apt)
        aid = await w.attempt(t, cid, "in_progress", paper=[q1, q2], answers={q1: 1, q2: 3},
                              deadline_min=-5)  # fmt: skip
        r = await _sign_in(w, cid)
        assert r.json()["rounds"][0]["attempt_status"] == "expired"
        entry_h = _h(r.json()["entry_token"])
        r = await w.c.post(f"{API}/entry/start", json={"round_id": str(apt)}, headers=entry_h)
        assert r.status_code == 409 and r.json()["error"]["code"] == "ATTEMPT_EXPIRED"
        assert "attempt_token" not in r.text
        (att,) = await w.sql(
            "select cast(status as text) as s, marks, time_taken_sec from attempts where id = :a",
            {"a": aid},
        )
        assert att["s"] == "submitted" and float(att["marks"]) == 5.0  # final at first contact
        assert 290 <= att["time_taken_sec"] <= 310  # capped at the deadline
        r = await w.c.get(f"{API}/entry/rounds", headers=entry_h)
        assert r.json()["rounds"][0]["attempt_status"] == "submitted"

    run(story)

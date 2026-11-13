"""Cut-off preview and advance to the next round (FR-12, AC-4). Scoring is NOT done here: an
attempt is finalized only through the existing submit (app/attempts/state_repo.submit), the same
code the student's final submit and the deadline auto-submit use."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.advance import ranking, repo
from app.advance.schemas import AdvanceIn, AdvanceOut, InvitedOut, RankingOut
from app.attempts import state_repo
from app.auth.schemas import StaffUser
from app.core.audit import audit
from app.core.errors import AppError

TEST_ROUNDS = ("aptitude", "coding", "technical")


def _owner(user: StaffUser) -> UUID | None:
    return None if user.role == "super_admin" else user.id


def _check_test_round(rnd) -> None:
    if rnd is None:
        raise AppError(404, "ROUND_NOT_FOUND", "Round not found")
    if rnd["type"] not in TEST_ROUNDS:
        raise AppError(409, "ROUND_HAS_NO_TEST", "Only test rounds are ranked by marks")


async def _finalize(session: AsyncSession, attempt_ids: list[UUID]) -> None:
    for attempt_id in attempt_ids:
        await state_repo.submit(session, attempt_id)


async def preview(
    session: AsyncSession, user: StaffUser, round_id: UUID, threshold: float | None
) -> RankingOut:
    rnd = await repo.get_round(session, round_id, _owner(user))
    _check_test_round(rnd)
    if rnd["status"] == "active":
        # the deadline already decided these; finalize like GET /attempt does (system, no audit)
        expired = await repo.attempts_past_deadline(session, round_id)
        if expired:
            await _finalize(session, expired)
            await session.commit()
    if threshold is None:
        threshold = rnd["threshold"]
    entries, counts = ranking.rank(await repo.round_rows(session, round_id), threshold)
    return RankingOut(
        round_id=round_id,
        round_status=rnd["status"],
        threshold=ranking.cut_off(threshold),
        counts=counts,
        candidates=entries,
    )


async def advance(
    session: AsyncSession, user: StaffUser, round_id: UUID, body: AdvanceIn
) -> AdvanceOut:
    """One transaction: every refusal raises before commit, so nothing is half done."""
    rnd = await repo.get_round(session, round_id, _owner(user), lock=True)
    _check_test_round(rnd)
    if rnd["status"] == "completed":
        raise AppError(409, "ROUND_ALREADY_ADVANCED", "This round was already advanced")
    if rnd["status"] != "active":
        raise AppError(409, "ROUND_NOT_ACTIVE", "Only an open round can be advanced")

    expired = await repo.attempts_past_deadline(session, round_id)
    await _finalize(session, expired)
    rows = await repo.round_rows(session, round_id)
    _, counts = ranking.rank(rows, body.threshold)
    if counts.still_writing:
        raise AppError(
            409,
            "ROUND_STILL_RUNNING",
            "Some students are still writing this test",
            {"counts": counts.model_dump(by_alias=True)},
        )
    locked = [r for r in rows if r["attempt_status"] == "locked"]
    if locked and not body.finalize_locked:
        raise AppError(
            409,
            "LOCKED_ATTEMPTS",
            "Some attempts are locked by tab switches. Resume them or finalize them first",
            {
                "attempts": [
                    {
                        "attempt_id": str(r["attempt_id"]),
                        "candidate_id": str(r["candidate_id"]),
                        "student_id": r["student_id"],
                        "name": r["name"],
                    }
                    for r in locked
                ]
            },
        )
    locked_ids = [r["attempt_id"] for r in locked]
    if locked_ids:
        await repo.reopen_locked(session, locked_ids)
        await _finalize(session, locked_ids)
        rows = await repo.round_rows(session, round_id)

    entries, counts = ranking.rank(rows, body.threshold)
    results = []
    for e in entries:  # only finished and absent are left at this point
        status = "absent" if e.percent is None else ("passed" if e.would_pass else "failed")
        results.append({"c": str(e.candidate_id), "s": status, "score": e.percent, "rank": e.rank})
    await repo.save_results(session, round_id, results)
    threshold = ranking.cut_off(body.threshold)
    await repo.complete_round(session, round_id, threshold)

    passed = [e for e in entries if e.would_pass]
    nxt = await repo.next_round(session, rnd["drive_id"], rnd["position"])
    invited: list[InvitedOut] = []
    if nxt is not None:
        await repo.open_round(session, nxt["id"])
        new_ids = set(await repo.invite(session, nxt["id"], [e.candidate_id for e in passed]))
        invited = [
            InvitedOut(candidate_id=e.candidate_id, student_id=e.student_id, name=e.name)
            for e in passed
            if e.candidate_id in new_ids
        ]

    out = AdvanceOut(
        round_id=round_id,
        threshold=threshold,
        passed=len(passed),
        failed=sum(1 for r in results if r["s"] == "failed"),
        absent=counts.absent,
        next_round_id=nxt["id"] if nxt is not None else None,
        invited=invited,
    )
    await audit(
        session,
        user.id,
        "round.advance",
        "round",
        round_id,
        {
            "threshold": threshold,
            "passed": out.passed,
            "failed": out.failed,
            "absent": out.absent,
            "finalized_expired": expired,
            "finalized_locked": locked_ids,
            "next_round_id": out.next_round_id,
            "invited": [i.candidate_id for i in invited],
        },
    )
    await session.commit()
    return out

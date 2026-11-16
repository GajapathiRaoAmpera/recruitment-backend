from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.attempts import repo
from app.attempts.schemas import StartOut
from app.auth import entry
from app.auth import repo as auth_repo
from app.core.errors import AppError
from app.papers import builder
from app.papers.schemas import PaperSettings, PoolQuestion


def _option_order(shown: list[str], original: list[str]) -> list[int]:
    """For each shown option, its index in the stored list (duplicates matched in order)."""
    left = list(range(len(original)))
    order = []
    for text in shown:
        i = next(i for i in left if original[i] == text)
        left.remove(i)
        order.append(i)
    return order


async def ensure_questions(
    session: AsyncSession, attempt_id: UUID, candidate_id: UUID, round_id: UUID, round_type: str
) -> bool:
    """Give this attempt its own paper (questions + option order) from the paper HR sent to
    the student. Repeating is harmless. False = no paper was sent to them."""
    if round_type == "coding":  # coding problems come from the round's test, set up by HR
        return await repo.coding_question_count(session, round_id) > 0
    if await repo.has_questions(session, attempt_id):
        return True
    paper = await repo.sent_paper(session, round_id, candidate_id)
    if paper is None:
        return False
    settings = PaperSettings.model_validate(paper["settings"])
    pool = [PoolQuestion.model_validate(q) for q in paper["pool"] if q.get("selected", True)]
    original = {q.id: q.options for q in pool}
    # same seed as the paper HR sent (drive + track + candidate), so it can be rebuilt
    items = builder.build_paper(settings, paper["drive_ref"], str(candidate_id), pool).items
    if not items:
        return False
    rows = [
        {"q": i.id, "p": n, "o": _option_order(i.options, original[i.id])}
        for n, i in enumerate(items)
    ]
    await repo.link_questions(session, attempt_id, rows)
    return True


async def start(session: AsyncSession, who: entry.Entry, round_id: UUID) -> StartOut:
    if await auth_repo.get_invite_target(session, who.candidate_id, round_id) is None:
        raise AppError(403, "NOT_INVITED", "You are not invited to this round")
    # invited, but not a round that can be sat right now (closed, finished, interview, ...)
    rnd = await repo.get_startable(session, who.candidate_id, round_id)
    if rnd is None:
        raise AppError(409, "ROUND_NOT_OPEN", "This round can't be started right now")
    existing = await repo.find_attempt(session, round_id, who.candidate_id)
    if existing is not None:
        # never reopen a finished attempt; HR's resume is what reopens a locked one
        if existing["status"] in ("submitted", "locked"):
            raise AppError(409, "ATTEMPT_LOCKED", "This attempt is already closed")
        if existing["status"] == "expired" or (
            existing["status"] == "in_progress" and existing["past_deadline"]
        ):
            raise AppError(409, "ATTEMPT_EXPIRED", "Time is up for this test")
    missing = await repo.missing_steps(session, who.candidate_id, round_id)
    if missing:
        raise AppError(409, "STEPS_MISSING", "Finish the system check first", {"missing": missing})
    test = await repo.ensure_test(session, round_id, rnd["minutes"])
    attempt_id = await repo.start_attempt(
        session, test["id"], who.candidate_id, round_id, test["duration_sec"]
    )
    if not await ensure_questions(session, attempt_id, who.candidate_id, round_id, rnd["type"]):
        # nothing is committed yet, so the attempt created above is rolled back
        raise AppError(
            409, "PAPER_NOT_READY", "Your test isn't ready yet. Please check back later, or ask HR."
        )
    await session.commit()
    return StartOut(
        attempt_token=entry.issue_attempt_token(attempt_id, who.candidate_id),
        attempt_id=attempt_id,
    )

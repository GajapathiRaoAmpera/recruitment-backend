"""Round 3 AI interview. A student HR moved into the open interview round signs in with Student ID +
DOB (repo.find_for_sign_in checks the invite). It does not read or change attempts or entry steps.
"""

from datetime import timedelta
from uuid import UUID

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import repo as auth_repo
from app.auth.entry import MAX_FAILURES
from app.auth.schemas import StaffUser
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import AppError
from app.interviews import frames, gemini, prompts, repo, scoring, tokens
from app.interviews.schemas import (
    MAX_TURNS_TOTAL,
    EvaluateOut,
    InterviewDetail,
    InterviewRow,
    SessionOut,
    StartIn,
    StartOut,
    TranscriptIn,
    TranscriptOut,
)

GRACE = timedelta(minutes=2)  # last turns posted just after the deadline still count
TOKEN_SLACK = timedelta(minutes=5)  # the Gemini token outlives the deadline by this much
NEW_SESSION_WINDOW = timedelta(minutes=2)  # the browser connects right after minting


async def reference_material(session: AsyncSession, ctx) -> str:
    """Role reference text for the interviewer (picks and checks technical questions).

    Seam for the planned Super Admin PDF knowledge base: retrieve the passages that match
    ctx["role_title"] / ctx["jd_text"] and return them as plain text (prompts caps the length).
    Empty until that exists; the interviewer then works from the job description alone.
    """
    return ""


def _not_found() -> AppError:
    return AppError(404, "INTERVIEW_NOT_FOUND", "Interview not found")


def _seconds_left(ctx) -> int | None:
    if ctx["deadline_at"] is None:
        return None
    return max(0, int((ctx["deadline_at"] - ctx["server_now"]).total_seconds()))


# ---------------------------------------------------------------------------------- student


async def start(session: AsyncSession, body: StartIn, meta: dict) -> StartOut:
    rows = await repo.find_for_sign_in(session, body.student_id, body.round_id)
    candidates = {r["candidate_id"] for r in rows}
    for cid in candidates:
        if await auth_repo.recent_failures(session, cid) >= MAX_FAILURES:
            raise AppError(429, "TOO_MANY_ATTEMPTS", "Too many attempts. Try again in 15 minutes")
    match = next((r for r in rows if r["dob"] == body.dob), None)
    if match is None:
        # same answer for "no such student", "wrong DOB" and "no interview round"
        logged = set()
        for r in rows:
            if r["candidate_id"] not in logged:
                logged.add(r["candidate_id"])
                await auth_repo.log_step(
                    session, r["candidate_id"], r["round_id"], "id_dob", False, meta
                )
        await session.commit()
        raise AppError(401, "ENTRY_MISMATCH", "Student ID or date of birth does not match")
    await auth_repo.log_step(
        session, match["candidate_id"], match["round_id"], "id_dob", True, meta
    )
    interview_id = await repo.ensure_interview(session, match["round_id"], match["candidate_id"])
    ctx = await repo.context(session, interview_id, match["candidate_id"])
    await session.commit()
    return StartOut(
        interview_token=tokens.issue(interview_id, match["candidate_id"]),
        interview_id=interview_id,
        status=ctx["status"],
        candidate_name=ctx["full_name"],
        role_title=ctx["role_title"],
        company=ctx["company"],
        duration_min=ctx["minutes"],
        seconds_left=_seconds_left(ctx),
    )


async def open_session(session: AsyncSession, who: tokens.InterviewAuth) -> SessionOut:
    ctx = await repo.context(session, who.interview_id, who.candidate_id)
    if ctx is None:
        raise _not_found()
    if ctx["status"] == "completed":
        raise AppError(409, "INTERVIEW_COMPLETED", "You've already finished this interview")
    if ctx["status"] == "failed":
        raise AppError(409, "INTERVIEW_CLOSED", "This interview was closed by HR")
    if ctx["deadline_at"] is not None and ctx["server_now"] >= ctx["deadline_at"]:
        raise AppError(409, "INTERVIEW_EXPIRED", "Time is up for this interview")
    if not get_settings().gemini_api_key:
        raise gemini.not_configured()  # before the clock starts
    earlier = await repo.transcript(session, who.interview_id) if ctx["turns"] else []
    overrides = await repo.prompt_overrides(session)
    await repo.mark_live(session, who.interview_id)
    ctx = await repo.context(session, who.interview_id, who.candidate_id)  # deadline now set
    left = _seconds_left(ctx) or 0
    instruction = prompts.build_system_instruction(
        company=ctx["company"],
        role=ctx["role_title"],
        name=ctx["full_name"],
        minutes=max(1, round(left / 60)) if earlier else ctx["minutes"],
        skills=list(ctx["skills"] or []),
        jd=ctx["jd_text"] or "",
        earlier_turns=earlier,
        reference=await reference_material(session, ctx),
        overrides=overrides,
    )
    expires = ctx["deadline_at"] + TOKEN_SLACK
    token, model = await gemini.mint_token(
        system_instruction=instruction,
        expire_at=expires,
        new_session_until=min(expires, ctx["server_now"] + NEW_SESSION_WINDOW),
    )
    await session.commit()  # only once the token exists: a failed mint doesn't start the clock
    return SessionOut(
        ephemeral_token=token,
        model=model,
        expires_at=expires,
        deadline_at=ctx["deadline_at"],
        server_now=ctx["server_now"],
        seconds_left=left,
        resumed=bool(earlier),
        role_title=ctx["role_title"],
        candidate_name=ctx["full_name"],
        client_config=gemini.client_config(get_settings().gemini_live_voice),
        protocol={
            "system_tag": prompts.SYSTEM_TAG,
            "typed_prefix": prompts.TYPED_PREFIX,
            "closing_phrase": prompts.CLOSING_PHRASE,
        },
    )


async def save_transcript(
    session: AsyncSession,
    who: tokens.InterviewAuth,
    body: TranscriptIn,
    background: BackgroundTasks,
) -> TranscriptOut:
    ctx = await repo.context(session, who.interview_id, who.candidate_id)
    if ctx is None:
        raise _not_found()
    if ctx["status"] == "completed":
        # a repeated final post (retry after a lost response) is harmless
        if body.ended and not body.turns:
            return TranscriptOut(saved=ctx["turns"], status="completed")
        raise AppError(409, "INTERVIEW_COMPLETED", "This interview is already finished")
    if ctx["status"] != "live":
        raise AppError(409, "INTERVIEW_NOT_LIVE", "Start the interview first")
    saved = ctx["turns"]
    late = ctx["server_now"] > ctx["deadline_at"] + GRACE
    if body.turns:
        if late:
            raise AppError(409, "INTERVIEW_EXPIRED", "Time is up for this interview")
        turns = [t.model_dump(mode="json") for t in body.turns]
        result = await repo.append_turns(session, who.interview_id, turns, MAX_TURNS_TOTAL)
        if result is None:
            raise AppError(
                422, "TRANSCRIPT_TOO_LONG", f"An interview stores at most {MAX_TURNS_TOTAL} turns"
            )
        saved = result
    status = "live"
    if body.ended and await repo.complete(session, who.interview_id):
        status = "completed"
        frames.drop(who.interview_id)
        background.add_task(scoring.run_scoring, who.interview_id)
    await session.commit()
    return TranscriptOut(saved=saved, status=status)


async def save_frame(session: AsyncSession, who: tokens.InterviewAuth, data: bytes) -> None:
    if len(data) > frames.MAX_BYTES:
        raise AppError(413, "FRAME_TOO_LARGE", "Camera image is too large")
    if not frames.is_jpeg(data):
        raise AppError(415, "FRAME_NOT_JPEG", "Camera image must be a JPEG")
    ctx = await repo.context(session, who.interview_id, who.candidate_id)
    if ctx is None:
        raise _not_found()
    if ctx["status"] != "live":
        raise AppError(409, "INTERVIEW_NOT_LIVE", "The interview isn't live")
    frames.put(who.interview_id, data)


# ----------------------------------------------------------------------------------------- HR


def _owner(user: StaffUser) -> UUID | None:
    return None if user.role == "super_admin" else user.id  # HR sees only their own drives


def _scoring(ai_scores) -> tuple[str | None, float | None]:
    s = repo.as_json(ai_scores)
    if not s:
        return None, None
    if s.get("pending"):
        return "pending", None
    if "error" in s:
        return "error", None
    keys = ("communication", "technical_depth", "problem_solving", "culture")
    try:
        return "done", round(sum(float(s[k]) for k in keys) / 4, 2)
    except (KeyError, TypeError, ValueError):
        return "error", None


def _row(r) -> dict:
    scoring_state, average = _scoring(r["ai_scores"])
    return {
        **{k: r[k] for k in InterviewRow.model_fields if k in r},
        "scoring": scoring_state,
        "average": average,
    }


async def list_interviews(
    session: AsyncSession, user: StaffUser, round_id: UUID | None, drive_id: UUID | None
) -> list[InterviewRow]:
    rows = await repo.list_for_hr(session, _owner(user), round_id, drive_id)
    return [InterviewRow.model_validate(_row(r)) for r in rows]


async def detail(session: AsyncSession, user: StaffUser, interview_id: UUID) -> InterviewDetail:
    r = await repo.get_for_hr(session, interview_id, _owner(user))
    if r is None:
        raise _not_found()
    return InterviewDetail.model_validate(
        {
            **_row(r),
            "transcript": repo.as_json(r["transcript"]) or [],
            "ai_scores": repo.as_json(r["ai_scores"]),
            "hr_notes": r["hr_notes"],
            "skills": list(r["skills"] or []),
            "deadline_at": r["deadline_at"],
        }
    )


async def frame(session: AsyncSession, user: StaffUser, interview_id: UUID) -> bytes:
    r = await repo.owned(session, interview_id, _owner(user))
    if r is None:
        raise _not_found()
    data = frames.get(interview_id) if r["status"] == "live" else None
    if data is None:
        raise AppError(404, "NO_FRAME", "No recent camera image")
    return data


async def evaluate(
    session: AsyncSession, user: StaffUser, interview_id: UUID, background: BackgroundTasks
) -> EvaluateOut:
    r = await repo.owned(session, interview_id, _owner(user))
    if r is None:
        raise _not_found()
    if not r["has_transcript"]:
        raise AppError(409, "NO_TRANSCRIPT", "There's no transcript to score yet")
    await repo.mark_pending(session, interview_id)
    await audit(session, user.id, "interview.evaluate", "interview", interview_id)
    await session.commit()
    background.add_task(scoring.run_scoring, interview_id)
    return EvaluateOut(scoring="pending")


async def set_notes(
    session: AsyncSession, user: StaffUser, interview_id: UUID, notes: str
) -> InterviewDetail:
    r = await repo.owned(session, interview_id, _owner(user))
    if r is None:
        raise _not_found()
    await repo.set_notes(session, interview_id, notes)
    await audit(
        session, user.id, "interview.notes", "interview", interview_id, {"chars": len(notes)}
    )
    await session.commit()
    return await detail(session, user, interview_id)

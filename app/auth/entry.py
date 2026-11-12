"""Student entry: Student ID + DOB checked against `candidates` -> 2 h entry token (no accounts)."""

import time
from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import repo
from app.auth.schemas import VerifyIn, VerifyOut
from app.auth.security import bearer
from app.core.config import get_settings
from app.core.db import get_session
from app.core.errors import AppError

ENTRY_TTL = 2 * 3600
MAX_FAILURES = 5  # wrong-DOB tries per candidate per 15 min


@dataclass
class Entry:
    candidate_id: UUID


def _decode(token: str) -> dict:
    try:
        # HS256 is fine: we issue and verify (the no-HS256 rule is for Supabase JWTs)
        claims = jwt.decode(
            token,
            get_settings().entry_signing_key,
            algorithms=["HS256"],
            options={"require": ["exp", "cid", "typ"]},
        )
        if claims["typ"] != "entry":
            raise jwt.InvalidTokenError("wrong token type")
        UUID(claims["cid"])
        return claims
    except (jwt.PyJWTError, ValueError) as e:
        raise AppError(401, "INVALID_TOKEN", "Sign in again") from e


async def verify(session: AsyncSession, body: VerifyIn, meta: dict) -> VerifyOut:
    rows = await repo.find_by_student_id(session, body.student_id)
    first: dict = {}  # candidate_id -> first row, for logging
    for r in rows:
        first.setdefault(r["candidate_id"], r)
    for cid in first:
        if await repo.recent_failures(session, cid) >= MAX_FAILURES:
            raise AppError(429, "TOO_MANY_ATTEMPTS", "Too many attempts. Try again in 15 minutes")
    for cid, r in first.items():
        await repo.log_step(session, cid, r["round_id"], "id_dob", r["dob"] == body.dob, meta)
    await session.commit()  # keep the logs even when we raise below
    # shortcut: same ID+DOB in two drives picks the newest candidate, upgrade if that happens
    mine = [r for r in rows if r["dob"] == body.dob]
    if (
        not mine
    ):  # same answer for "no such student" and "wrong DOB"; unknown IDs can't be logged (FK)
        raise AppError(401, "ENTRY_MISMATCH", "Student ID or date of birth does not match")
    cid = mine[0]["candidate_id"]
    token = jwt.encode(
        {"typ": "entry", "cid": str(cid), "exp": time.time() + ENTRY_TTL},
        get_settings().entry_signing_key,
        algorithm="HS256",
    )
    return VerifyOut(
        entry_token=token,
        candidate={"full_name": mine[0]["full_name"]},
        rounds=[
            {
                "round_id": str(r["round_id"]),
                "type": r["round_type"],
                "test_id": str(r["test_id"]) if r["test_id"] else None,
            }
            for r in mine
            if r["candidate_id"] == cid
        ],
    )


async def current_entry(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> Entry:
    if creds is None:
        raise AppError(401, "UNAUTHENTICATED", "Verify your identity first")
    return Entry(UUID(_decode(creds.credentials)["cid"]))

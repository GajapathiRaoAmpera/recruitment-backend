"""Student auth for the standalone interview: a token scoped to ONE interview of ONE candidate.

Same signing key as the entry/attempt tokens, its own `typ`, so neither of those works here and
this one works nowhere else. Kept separate from the attempt pipeline on purpose (for now).
"""

import time
from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.security import bearer
from app.core.config import get_settings
from app.core.errors import AppError

TTL = 3 * 3600  # longer than any interview, so a reload mid-interview needs no new sign-in


@dataclass(frozen=True)
class InterviewAuth:
    interview_id: UUID
    candidate_id: UUID


def issue(interview_id: UUID, candidate_id: UUID) -> str:
    claims = {
        "typ": "interview",
        "iid": str(interview_id),
        "cid": str(candidate_id),
        "exp": time.time() + TTL,
    }
    return jwt.encode(claims, get_settings().entry_signing_key, algorithm="HS256")


def decode(token: str) -> InterviewAuth:
    try:
        claims = jwt.decode(
            token,
            get_settings().entry_signing_key,
            algorithms=["HS256"],
            options={"require": ["exp", "iid", "cid", "typ"]},
        )
        if claims["typ"] != "interview":
            raise jwt.InvalidTokenError("wrong token type")
        return InterviewAuth(UUID(claims["iid"]), UUID(claims["cid"]))
    except (jwt.PyJWTError, ValueError) as e:
        raise AppError(401, "INVALID_TOKEN", "Sign in again") from e


async def current_interview(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> InterviewAuth:
    if creds is None:
        raise AppError(401, "UNAUTHENTICATED", "Sign in first")
    return decode(creds.credentials)

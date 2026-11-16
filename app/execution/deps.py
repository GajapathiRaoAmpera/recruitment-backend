"""Who is calling the student endpoints (Auth S: the student attempt token).

The attempt token is issued by `/entry/start` and verified by `current_attempt`
(app/auth/entry.py); this function turns it into an `AttemptContext`. Tests override it.
"""

from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

from fastapi import Depends

from app.auth.entry import current_attempt
from app.execution.judge0 import Judge0Client


@dataclass(frozen=True)
class AttemptContext:
    attempt_id: UUID
    candidate_id: UUID


async def student_attempt(who: tuple[UUID, UUID] = Depends(current_attempt)) -> AttemptContext:
    attempt_id, candidate_id = who
    return AttemptContext(attempt_id=attempt_id, candidate_id=candidate_id)


@lru_cache
def judge0_client() -> Judge0Client:
    return Judge0Client()

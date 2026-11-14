"""Who is calling the student endpoints (Auth S: the student attempt token).

The attempt token is issued by `/entry/start` (plan 1.3), which is not built yet. Until it
lands, `student_attempt` refuses every request, so the run / submit-code routes are safe to
ship but unusable. When 1.3 is in, make this function verify that token and return the
`AttemptContext`; nothing else in app/execution has to change. Tests override it.
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

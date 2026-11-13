"""Cut-off preview (GET /rounds/{id}/ranking) and advance (POST /rounds/{id}/advance). HR only."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

AttemptStatus = Literal["not_started", "in_progress", "locked", "submitted", "expired"]


class RankingEntry(BaseModel):
    candidate_id: UUID
    student_id: str
    name: str
    attempt_status: AttemptStatus | None  # None = never started
    marks: float | None
    max_marks: float | None  # this student's own paper; papers can differ per student
    percent: float | None
    rank: int | None  # finished attempts only
    time_taken_sec: int | None
    would_pass: bool | None  # None while still writing / locked, or with no threshold


class Counts(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    pass_: int | None = Field(alias="pass")  # None when there is no threshold
    fail: int | None
    absent: int
    still_writing: int
    locked: int


class RankingOut(BaseModel):
    round_id: UUID
    round_status: str
    threshold: float | None
    counts: Counts
    candidates: list[RankingEntry]


class AdvanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: float = Field(ge=0, le=100)  # percent
    finalize_locked: bool = False  # submit tab-switch-locked attempts with the marks they have


class InvitedOut(BaseModel):
    candidate_id: UUID
    student_id: str
    name: str


class AdvanceOut(BaseModel):
    round_id: UUID
    threshold: float
    passed: int
    failed: int
    absent: int
    next_round_id: UUID | None
    invited: list[InvitedOut]  # for a reminder email later (no SMTP yet)

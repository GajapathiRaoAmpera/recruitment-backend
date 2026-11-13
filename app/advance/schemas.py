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


# ---- HR-decided rounds (ai_interview, final): the AI recommends, HR decides (FR-20, FR-22)


class DecisionRow(BaseModel):
    candidate_id: UUID
    student_id: str
    name: str
    status: str  # round_candidates.status
    interview_id: UUID | None
    interview_status: str | None  # scheduled | live | completed | failed, None = never signed in
    scoring: Literal["pending", "done", "error"] | None
    ai_average: float | None  # 1-5 AI recommendation, never a decision


class DecisionsOut(BaseModel):
    round_id: UUID
    round_type: str
    round_status: str
    candidates: list[DecisionRow]


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    decision: Literal["pass", "fail"]  # final round: pass = selected, fail = rejected


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[DecisionIn] = Field(max_length=5000)  # one per student in the round


class DecideOut(BaseModel):
    round_id: UUID
    passed: int
    failed: int
    next_round_id: UUID | None
    invited: list[InvitedOut]
    drive_closed: bool  # True when this was the last round

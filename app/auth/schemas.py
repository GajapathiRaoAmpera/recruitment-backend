from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

Role = Literal["hr", "super_admin"]


class StaffUser(BaseModel):
    """The signed-in HR/Admin user; also the GET /me response."""

    id: UUID
    email: str
    full_name: str
    role: Role


class LoginIn(BaseModel):
    email: str
    password: str


class LoginOut(BaseModel):
    access_token: str
    expires_in: int  # seconds
    user: StaffUser


class ForgotIn(BaseModel):
    email: str


class SetPasswordIn(BaseModel):
    token: str
    password: str


class VerifyIn(BaseModel):
    student_id: str
    dob: date


AttemptStatus = Literal["not_started", "in_progress", "locked", "submitted", "expired"]


class EntryRound(BaseModel):
    """One round the student is in. Same list from POST /entry/verify and GET /entry/rounds."""

    round_id: str
    type: str
    test_id: str | None
    duration_min: float | None
    attempt_status: AttemptStatus  # in_progress past its deadline reads "expired"


class VerifyOut(BaseModel):
    entry_token: str
    candidate: dict[str, str]
    rounds: list[EntryRound]


class EntryRoundsOut(BaseModel):
    rounds: list[EntryRound]


class StepIn(BaseModel):
    round_id: UUID
    step: Literal["location", "terms", "fullscreen"]
    ok: bool
    meta: dict = {}


class ConfirmIn(BaseModel):
    """Per-round "confirm it's you": the sign-in ID + DOB check, logged against this round."""

    round_id: UUID
    student_id: str
    dob: date

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


class VerifyIn(BaseModel):
    student_id: str
    dob: date


class VerifyOut(BaseModel):
    entry_token: str
    candidate: dict[str, str]
    rounds: list[dict[str, str | float | None]]  # round_id, type, test_id, duration_min


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

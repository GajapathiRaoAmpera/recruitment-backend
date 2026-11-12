from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class QuestionOut(BaseModel):
    id: UUID
    body: str
    options: list[str]
    marks: float


class AttemptOut(BaseModel):
    status: Literal["in_progress", "locked", "submitted", "expired", "not_started"]
    deadline_at: datetime
    server_now: datetime  # the client shows deadline_at - server_now, never its own clock
    tab_switches: int
    max_tab_switches: int
    remaining_sec_on_lock: int | None
    questions: list[QuestionOut]
    saved_answers: dict[str, int]


class SubmitOut(BaseModel):
    status: Literal["submitted"]
    time_taken_sec: int | None  # marks are not returned: students never see them


class AnswerIn(BaseModel):
    response: int = Field(ge=0, le=20)

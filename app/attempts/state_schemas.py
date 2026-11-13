from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class QuestionOut(BaseModel):
    """A multiple-choice question (kept as it was; `kind` is new and always "mcq")."""

    kind: Literal["mcq"] = "mcq"
    id: UUID
    body: str
    options: list[str]
    marks: float


class LanguageOut(BaseModel):
    key: str  # what /attempt/run and /attempt/submit-code accept
    name: str


class SampleCase(BaseModel):
    """Only sample cases ever reach a student; hidden cases have no field here."""

    input: str
    expected: str


class CodingQuestionOut(BaseModel):
    kind: Literal["coding"] = "coding"
    id: UUID
    title: str
    statement: str
    marks: float
    languages: list[LanguageOut]
    samples: list[SampleCase]  # at most 3


class SavedCode(BaseModel):
    code: str
    language: str | None


class AttemptOut(BaseModel):
    status: Literal["in_progress", "locked", "submitted", "expired", "not_started"]
    deadline_at: datetime
    server_now: datetime  # the client shows deadline_at - server_now, never its own clock
    tab_switches: int
    max_tab_switches: int
    remaining_sec_on_lock: int | None
    questions: list[QuestionOut | CodingQuestionOut]
    saved_answers: dict[str, int | SavedCode]  # mcq: chosen option; coding: last submitted code


class SubmitOut(BaseModel):
    status: Literal["submitted"]
    time_taken_sec: int | None  # marks are not returned: students never see them


class AnswerIn(BaseModel):
    response: int = Field(ge=0, le=20)

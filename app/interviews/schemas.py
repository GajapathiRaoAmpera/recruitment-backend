"""Round 3 AI interview: wire shapes and the schema every AI score passes before it is stored."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TURNS_PER_CALL = 50
MAX_TURN_CHARS = 2000  # a spoken turn
MAX_TYPED_CHARS = 8000  # a typed answer (code fits)
MAX_TURNS_TOTAL = 1500
MAX_NOTES_CHARS = 5000


# ---------------------------------------------------------------------------------- student


class StartIn(BaseModel):
    """Standalone sign-in for the interview (not tied to the round pipeline yet)."""

    student_id: str = Field(min_length=1, max_length=64)
    dob: date
    round_id: UUID | None = None  # from the invite link; omitted = the drive's AI interview round


class StartOut(BaseModel):
    interview_token: str
    interview_id: UUID
    status: str  # scheduled | live | completed
    candidate_name: str
    role_title: str
    company: str
    duration_min: int
    seconds_left: int | None  # None until the interview has started


class SessionOut(BaseModel):
    """What the browser needs to open its own Gemini Live connection. Never the key or prompt."""

    ephemeral_token: str
    model: str
    expires_at: datetime
    deadline_at: datetime
    server_now: datetime
    seconds_left: int
    resumed: bool  # true when earlier turns exist (a reload): the interviewer must not greet again
    role_title: str
    candidate_name: str
    # what the browser sends at connect: transport settings + tool declarations, never the prompt
    client_config: dict
    # phrases shared with the prompt: platform tag, typed-answer prefix, closing phrase
    protocol: dict[str, str]


class TurnIn(BaseModel):
    role: Literal["ai", "candidate"]
    # spoken = live captions; typed = the candidate's answer box (code or text); question = the
    # question the interviewer put in the answer box
    kind: Literal["spoken", "typed", "question"] = "spoken"
    text: str = Field(min_length=1, max_length=MAX_TYPED_CHARS)
    language: str | None = Field(None, max_length=32)  # typed code only, e.g. "python"
    ts: datetime

    @field_validator("text")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be blank")
        return v.rstrip()  # leading spaces matter in code

    @model_validator(mode="after")
    def _limits(self) -> "TurnIn":
        if self.kind == "spoken":
            self.text = self.text.strip()
            if len(self.text) > MAX_TURN_CHARS:
                raise ValueError(f"a spoken turn is at most {MAX_TURN_CHARS} characters")
        if self.kind == "typed" and self.role != "candidate":
            raise ValueError("only the candidate types answers")
        if self.kind == "question" and self.role != "ai":
            raise ValueError("only the interviewer asks answer-box questions")
        if self.kind != "typed":
            self.language = None
        return self


class TranscriptIn(BaseModel):
    turns: list[TurnIn] = Field(default_factory=list, max_length=MAX_TURNS_PER_CALL)
    ended: bool = False


class TranscriptOut(BaseModel):
    saved: int  # turns stored in total
    status: str  # live | completed


# -------------------------------------------------------------------------------- AI scores

Score = int


class Sentiment(BaseModel):
    overall: Literal["positive", "neutral", "negative"]
    confidence: Literal["low", "medium", "high"]
    note: str = Field(max_length=300)


class GeneratedScores(BaseModel):
    """The model's reply. Anything outside this shape is rejected (D10, AC-8)."""

    communication: Score = Field(ge=1, le=5)
    technical_depth: Score = Field(ge=1, le=5)
    problem_solving: Score = Field(ge=1, le=5)
    culture: Score = Field(ge=1, le=5)
    sentiment: Sentiment
    summary: str = Field(min_length=1, max_length=1200)

    @field_validator(
        "communication", "technical_depth", "problem_solving", "culture", mode="before"
    )
    @classmethod
    def _whole_number(cls, v: Any) -> Any:
        # bools are ints in Python, and 3.5 would be silently truncated: both are refused
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("score must be a whole number from 1 to 5")
        return v


class AiScores(GeneratedScores):
    model: str
    evaluated_at: datetime


# ------------------------------------------------------------------------------------- HR

ScoringState = Literal["pending", "done", "error"]


class InterviewRow(BaseModel):
    id: UUID
    round_id: UUID
    drive_id: UUID
    company: str
    role_title: str
    candidate_id: UUID
    student_id: str
    full_name: str
    status: str  # scheduled | live | completed | failed
    started_at: datetime | None
    ended_at: datetime | None
    turns: int
    scoring: ScoringState | None  # None = never scored
    average: float | None  # mean of the four rubric scores, 1-5
    has_notes: bool


class InterviewDetail(InterviewRow):
    transcript: list[dict]
    ai_scores: dict | None
    hr_notes: str | None
    skills: list[str]
    deadline_at: datetime | None


class NotesIn(BaseModel):
    hr_notes: str = Field(max_length=MAX_NOTES_CHARS)


class EvaluateOut(BaseModel):
    scoring: ScoringState

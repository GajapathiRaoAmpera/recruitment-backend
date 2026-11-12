"""Schemas for the Round 1 paper builder."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

Track = Literal["it", "non_it"]
Level = Literal["easy", "medium", "advanced"]
Section = Literal["aptitude", "reasoning", "technical"]
DraftStatus = Literal["generating", "draft", "accepted", "failed"]

LEVELS: tuple[Level, ...] = ("easy", "medium", "advanced")
SECTIONS: tuple[Section, ...] = ("aptitude", "reasoning", "technical")


def _require_keys(value: dict, keys: tuple[str, ...], name: str) -> None:
    if set(value) != set(keys):
        raise ValueError(f"{name} must have exactly the keys: {', '.join(keys)}")
    if any(v < 0 or v > 100 for v in value.values()):
        raise ValueError(f"each {name} value must be between 0 and 100")


class PaperSettings(BaseModel):
    track: Track
    counts: dict[Level, int]
    mix: dict[Section, int]
    it_syntax_percent: int = Field(default=50, ge=0, le=100)
    pool_factor: int = Field(default=2, ge=1, le=3)
    duration_min: int = Field(default=60, ge=5, le=240)
    different_per_student: bool = True
    shuffle_questions: bool = True
    shuffle_options: bool = True
    # IT Technical from the PDF banks: subject weights (None = even split) and a retrieval focus
    subjects: dict[str, int] | None = None
    focus: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check(self) -> "PaperSettings":
        _require_keys(self.counts, LEVELS, "counts")
        _require_keys(self.mix, SECTIONS, "mix")
        if sum(self.counts.values()) < 1:
            raise ValueError("counts must total at least 1")
        if sum(self.mix.values()) != 100:
            raise ValueError("mix must sum to 100")
        if self.subjects is not None:
            if any(not k.strip() for k in self.subjects):
                raise ValueError("subject names must not be blank")
            if any(v < 0 or v > 100 for v in self.subjects.values()):
                raise ValueError("each subject weight must be between 0 and 100")
            if sum(self.subjects.values()) != 100:
                raise ValueError("subject weights must sum to 100")
        if self.focus is not None:
            self.focus = " ".join(self.focus.split()) or None
        return self


class PoolQuestion(BaseModel):
    id: str
    level: Level
    section: Section
    topic: str
    style: str  # concept | syntax | soft_skill | aptitude | reasoning
    text: str
    options: list[str]
    answer: int
    why: str
    fixed_options: bool = False
    edited: bool = False
    verified: bool = True
    selected: bool = True  # HR's pick: only selected questions reach students
    source_id: str | None = None  # question-bank question this was copied from (IT Technical)


class PaperItem(BaseModel):
    """Student-safe: never carries the answer."""

    id: str
    level: Level
    section: Section
    topic: str
    text: str
    options: list[str]


class KeyItem(PaperItem):
    answer: int
    why: str
    fixed_options: bool = False
    edited: bool = False


class PoolItem(KeyItem):
    """A pool question as HR reviews it (original option order), with HR's selection."""

    selected: bool = True


class QuestionEdit(BaseModel):
    text: str
    options: list[str]
    answer: int
    why: str

    @model_validator(mode="after")
    def _check(self) -> "QuestionEdit":
        self.text = self.text.strip()
        self.why = self.why.strip()
        self.options = [o.strip() for o in self.options]
        if not self.text:
            raise ValueError("text must not be blank")
        if not self.why:
            raise ValueError("why must not be blank")
        if len(self.options) != 4:
            raise ValueError("exactly 4 options are required")
        if any(not o for o in self.options):
            raise ValueError("options must not be blank")
        if len({o.casefold() for o in self.options}) != 4:
            raise ValueError("options must be distinct")
        if not 0 <= self.answer < 4:
            raise ValueError("answer must be an index from 0 to 3")
        return self


class Check(BaseModel):
    id: str
    label: str
    ok: bool
    detail: str


# --------------------------------------------------------------------------------------------
# API DTOs (HR / Super Admin only; KeyItem answers are allowed here, never in student schemas)
# --------------------------------------------------------------------------------------------

MAX_CANDIDATES_PER_SEND = 5000
MAX_POOL_SELECTION = 5000  # far above the largest pool (3 x 300 questions + reserve)


class DraftCreate(BaseModel):
    drive_ref: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    settings: PaperSettings

    @model_validator(mode="after")
    def _strip(self) -> "DraftCreate":
        self.drive_ref = self.drive_ref.strip()
        self.title = self.title.strip()
        if not self.drive_ref:
            raise ValueError("drive_ref must not be blank")
        if not self.title:
            raise ValueError("title must not be blank")
        return self


class Progress(BaseModel):
    done: int = 0
    total: int = 0


class DraftOut(BaseModel):
    id: UUID
    drive_ref: str
    owner_id: UUID
    title: str
    track: Track
    status: DraftStatus
    settings: PaperSettings
    progress: Progress
    error: str | None = None
    pool_size: int = 0
    created_at: datetime
    accepted_at: datetime | None = None
    persisted_at: datetime | None = None


class OverlapOut(BaseModel):
    """Two sample students ("sample-a" vs "sample-b"): how many questions they share."""

    shared: int
    same_position: int
    total: int


class IssueOut(BaseModel):
    candidate_ref: str
    issued_at: datetime


class DraftDetail(DraftOut):
    checks: list[Check] = Field(default_factory=list)
    sample: list[KeyItem] = Field(default_factory=list)  # the one review paper ("sample")
    overlap: OverlapOut | None = None
    issues: list[IssueOut] = Field(default_factory=list)
    edited_count: int = 0
    pool: list[PoolItem] = Field(default_factory=list)  # every generated question, for selection
    selected_count: int = 0


class SelectionUpdate(BaseModel):
    selected_ids: list[str] = Field(max_length=MAX_POOL_SELECTION)


class SendRequest(BaseModel):
    candidate_refs: list[str] = Field(max_length=MAX_CANDIDATES_PER_SEND)

    @model_validator(mode="after")
    def _check(self) -> "SendRequest":
        if any(len(r) > 200 for r in self.candidate_refs):
            raise ValueError("each candidate_ref must be at most 200 characters")
        return self


class SendResult(BaseModel):
    sent: int
    already_sent: int


class AiStatus(BaseModel):
    gemini: bool
    groq: bool

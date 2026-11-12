"""Question-bank API DTOs (HR / Super Admin only: bank questions carry their answers here)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.papers.schemas import Level, Track

MAX_PAGE_SIZE = 100
MAX_APPROVAL_IDS = 5000
NAME_MAX = 200
NOTES_MAX = 2000

EmbeddingStatus = Literal["pending", "done", "failed", "skipped"]
SourceStatus = Literal["ready", "failed"]
ApprovalScope = Literal["all_clean", "subject", "ids"]


class SourceOut(BaseModel):
    """A bank file as listed. storage_key never leaves the server."""

    id: UUID
    name: str
    filename: str
    sha256: str
    track: Track
    notes: str | None = None
    status: SourceStatus
    is_active: bool
    uploaded_by: UUID
    embedding_status: EmbeddingStatus
    created_at: datetime
    updated_at: datetime
    question_count: int = 0
    approved_count: int = 0
    can_manage: bool = False  # the caller may edit / approve / replace / delete it


class SourcePage(BaseModel):
    items: list[SourceOut]
    total: int
    page: int
    page_size: int


class ReportOut(BaseModel):
    """The parser's ParseReport, as stored with the bank."""

    total: int = 0
    by_subject: dict[str, int] = Field(default_factory=dict)
    by_level: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class CellCount(BaseModel):
    total: int
    approved: int


class SourceDetail(SourceOut):
    report: ReportOut
    counts: dict[str, dict[Level, CellCount]] = Field(default_factory=dict)  # subject -> level


class BankQuestionOut(BaseModel):
    id: UUID
    source_id: UUID
    subject: str | None = None
    topic: str | None = None
    level: Level
    text: str
    options: list[str]
    answer: int
    explanation: str | None = None
    approved: bool
    warnings: list[str] = Field(default_factory=list)
    source_number: int | None = None
    created_at: datetime


class QuestionPage(BaseModel):
    items: list[BankQuestionOut]
    total: int
    page: int
    page_size: int


class SourcePatch(BaseModel):
    """Metadata only; the file is replaced through PUT /{id}/file."""

    name: str | None = Field(default=None, max_length=NAME_MAX)
    track: Track | None = None
    notes: str | None = Field(default=None, max_length=NOTES_MAX)
    is_active: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> "SourcePatch":
        if not self.model_fields_set:
            raise ValueError("send at least one of name, track, notes, is_active")
        for f in ("name", "track", "is_active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("name must not be blank")
        if self.notes is not None:
            self.notes = self.notes.strip() or None
        return self


class ApprovalUpdate(BaseModel):
    """all_clean: every question without parse warnings (approve) / every question (unapprove);
    subject: every question of one subject; ids: exactly these questions of this bank."""

    approved: bool
    scope: ApprovalScope
    subject: str | None = Field(default=None, max_length=NAME_MAX)
    question_ids: list[UUID] | None = Field(default=None, max_length=MAX_APPROVAL_IDS)

    @model_validator(mode="after")
    def _check(self) -> "ApprovalUpdate":
        if self.scope == "subject":
            self.subject = (self.subject or "").strip() or None
            if self.subject is None:
                raise ValueError("subject is required for scope 'subject'")
        elif self.subject is not None:
            raise ValueError("subject is only allowed with scope 'subject'")
        if self.scope == "ids":
            if not self.question_ids:
                raise ValueError("question_ids is required for scope 'ids'")
        elif self.question_ids is not None:
            raise ValueError("question_ids is only allowed with scope 'ids'")
        return self


class ApprovalResult(BaseModel):
    changed: int  # questions matched by the scope (set to `approved`)
    question_count: int
    approved_count: int


class Availability(BaseModel):
    """Approved questions in active, ready banks of one track (what a paper can draw on)."""

    track: Track
    subjects: dict[str, dict[Level, int]]  # every level present, zeros included
    by_level: dict[Level, int]
    total: int

"""Coding results for HR (HR / Super Admin only). Pass counts and HR's own submissions' code;
hidden test case text never appears here."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

Difficulty = Literal["easy", "medium", "advanced"]


class ResultQuestion(BaseModel):
    question_id: UUID
    position: int
    difficulty: Difficulty
    marks: Decimal


class ResultAnswer(BaseModel):
    question_id: UUID
    submitted: bool  # false = the student never pressed Submit on this question
    language: str | None
    passed: int | None
    total: int | None
    marks_earned: Decimal  # marks x passed / total, unless an explicit mark was stored


class CandidateResult(BaseModel):
    attempt_id: UUID
    candidate_id: UUID
    student_id: str
    full_name: str
    status: str  # in_progress / submitted / locked / expired ... (expired = past the deadline)
    started_at: datetime | None
    submitted_at: datetime | None
    score: Decimal
    answers: list[ResultAnswer]


class TestResults(BaseModel):
    __test__ = False
    test_id: UUID
    max_score: Decimal
    questions: list[ResultQuestion]
    candidates: list[CandidateResult]


class Submission(ResultAnswer):
    code: str | None
    updated_at: datetime | None


class AttemptSubmissions(BaseModel):
    attempt_id: UUID
    candidate_id: UUID
    student_id: str
    full_name: str
    status: str
    score: Decimal
    max_score: Decimal
    submissions: list[Submission]

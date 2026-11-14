"""Test set-up shapes (HR / Super Admin only)."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Difficulty = Literal["easy", "medium", "advanced"]

MAX_QUESTIONS = 50


class TestConfigIn(BaseModel):
    """PUT /rounds/{id}/test. Only the fields sent change; the question list and
    question_count are set through PUT /tests/{id}/questions."""

    __test__ = False  # not a pytest class
    model_config = ConfigDict(extra="forbid")

    difficulty: Difficulty | None = None
    duration_sec: int | None = Field(default=None, ge=60, le=36_000)
    opens_at: datetime | None = None
    closes_at: datetime | None = None

    @model_validator(mode="after")
    def _window(self):
        if self.opens_at and self.closes_at and self.closes_at <= self.opens_at:
            raise ValueError("closes_at must be after opens_at")
        return self


class TestConfigOut(BaseModel):
    __test__ = False
    id: UUID
    round_id: UUID
    difficulty: Difficulty | None
    duration_sec: int
    question_count: int
    settings: dict
    opens_at: datetime | None
    closes_at: datetime | None


class RoundTestOut(TestConfigOut):
    """GET /rounds/{id}/test: the PUT's shape plus the question list and the lock."""

    __test__ = False
    question_ids: list[UUID]  # in the order students see
    locked: bool  # a student has started, so the questions can't change


class TestQuestionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # order is the order students see; an empty list clears the test
    question_ids: list[UUID] = Field(max_length=MAX_QUESTIONS)

    @model_validator(mode="after")
    def _unique(self):
        if len(set(self.question_ids)) != len(self.question_ids):
            raise ValueError("question_ids must not repeat a question")
        return self


class TestQuestionOut(BaseModel):
    __test__ = False
    question_id: UUID
    position: int
    difficulty: Difficulty
    marks: Decimal


class TestQuestionsOut(BaseModel):
    __test__ = False
    test_id: UUID
    question_count: int
    levels: dict[Difficulty, int]  # how many questions of each level
    questions: list[TestQuestionOut]

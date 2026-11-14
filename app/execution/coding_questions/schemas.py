"""Coding question authoring (HR / Super Admin only). These shapes carry the hidden test cases,
so they must never be used in a student-facing response."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.execution.function_mode import values
from app.execution.function_mode.signature import Mode, Signature

Difficulty = Literal["easy", "medium", "advanced"]

SAMPLE_CASES = 3
HIDDEN_CASES = 15
MAX_CASE_CHARS = 20_000
MAX_BODY_CHARS = 20_000
MAX_TAGS = 10


class CaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(max_length=MAX_CASE_CHARS)  # stdin; may be empty
    expected: str = Field(min_length=1, max_length=MAX_CASE_CHARS)
    is_sample: bool

    @field_validator("expected")
    @classmethod
    def _expected_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("expected output must not be blank")
        return v


def _check_cases(cases: list[CaseIn]) -> list[CaseIn]:
    samples = sum(c.is_sample for c in cases)
    hidden = len(cases) - samples
    if (samples, hidden) != (SAMPLE_CASES, HIDDEN_CASES):
        raise ValueError(
            f"a coding question needs exactly {SAMPLE_CASES} sample and {HIDDEN_CASES} hidden "
            f"test cases, got {samples} sample and {hidden} hidden"
        )
    return cases


def canonical_cases(signature: Signature, cases: list[CaseIn]) -> list[CaseIn]:
    """Function mode: each input must hold one value per parameter and each expected one
    return value; both are stored in the canonical form the drivers read (values.py).
    Raises ValueError naming the case."""
    out = []
    for i, c in enumerate(cases, start=1):
        try:
            inp = values.canonical_input(signature, c.input)
            exp = values.canonical_expected(signature, c.expected)
        except ValueError as e:
            raise ValueError(f"test case {i}: {e}") from None
        out.append(CaseIn(input=inp, expected=exp, is_sample=c.is_sample))
    return out


def _check_mode(mode: str, signature: Signature | None) -> None:
    if mode == "function" and signature is None:
        raise ValueError("a function-mode question needs a signature")
    if mode == "stdin" and signature is not None:
        raise ValueError("signature is only for mode 'function'")


def _clean_tags(tags: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(t.strip() for t in tags if t.strip()))  # keeps order, no dupes
    if len(cleaned) > MAX_TAGS or any(len(t) > 40 for t in cleaned):
        raise ValueError(f"at most {MAX_TAGS} tags of up to 40 characters")
    return cleaned


class CodingQuestionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["coding"]
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    difficulty: Difficulty
    tags: list[str] = []
    marks: Decimal = Field(default=Decimal(1), gt=0, le=999, decimal_places=2)
    mode: Mode = "stdin"
    signature: Signature | None = None  # required for (and only for) mode "function"
    test_cases: list[CaseIn]

    @field_validator("body")
    @classmethod
    def _body_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be blank")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return _clean_tags(v)

    @field_validator("test_cases")
    @classmethod
    def _cases(cls, v: list[CaseIn]) -> list[CaseIn]:
        return _check_cases(v)

    @model_validator(mode="after")
    def _function_mode(self) -> "CodingQuestionIn":
        _check_mode(self.mode, self.signature)
        if self.signature is not None:
            self.test_cases = canonical_cases(self.signature, self.test_cases)
        return self


class CodingQuestionPatch(BaseModel):
    """Only the fields sent change. `test_cases`, when sent, replaces the whole set.
    `mode: "stdin"` clears the signature; `mode: "function"` needs a signature (sent now, or
    already stored). The service checks the resulting mode / signature / cases together."""

    model_config = ConfigDict(extra="forbid")

    body: str | None = Field(default=None, min_length=1, max_length=MAX_BODY_CHARS)
    difficulty: Difficulty | None = None
    tags: list[str] | None = None
    marks: Decimal | None = Field(default=None, gt=0, le=999, decimal_places=2)
    approved: bool | None = None
    mode: Mode | None = None
    signature: Signature | None = None
    test_cases: list[CaseIn] | None = None

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_tags(v)

    @field_validator("test_cases")
    @classmethod
    def _cases(cls, v: list[CaseIn] | None) -> list[CaseIn] | None:
        return None if v is None else _check_cases(v)

    @model_validator(mode="after")
    def _mode_and_signature(self) -> "CodingQuestionPatch":
        if self.mode == "stdin" and "signature" in self.model_fields_set:
            raise ValueError("signature is only for mode 'function'")
        return self

    @model_validator(mode="after")
    def _something_to_change(self) -> "CodingQuestionPatch":
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        if any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError("fields that are sent cannot be null")
        return self


class CaseOut(BaseModel):
    id: UUID
    input: str
    expected: str
    is_sample: bool
    position: int


class CodingQuestionOut(BaseModel):
    id: UUID
    kind: Literal["coding"] = "coding"
    body: str
    difficulty: Difficulty
    tags: list[str]
    marks: Decimal
    approved: bool
    created_by: UUID | None
    created_at: datetime
    # AI questions: the program whose Judge0 stdout became `expected`. Null for manual ones.
    reference_solution: str | None = None
    reference_language: str | None = None
    mode: Mode = "stdin"
    signature: Signature | None = None  # set exactly when mode is "function"
    test_cases: list[CaseOut]


class GenerateIn(BaseModel):
    """POST /questions/generate: AI drafts, saved with approved = false."""

    model_config = ConfigDict(extra="forbid")

    difficulty: Difficulty
    topic: str | None = Field(default=None, max_length=200)
    tags: list[str] = []
    marks: Decimal = Field(default=Decimal(1), gt=0, le=999, decimal_places=2)
    count: int = Field(default=1, ge=1, le=3)
    mode: Mode = "stdin"  # "function": the model also writes the signature

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: str | None) -> str | None:
        return (v.strip() or None) if v is not None else None

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return _clean_tags(v)


class GenerateFailure(BaseModel):
    index: int  # 0-based position in the request's count
    reason: str


class GenerateOut(BaseModel):
    created: list[CodingQuestionOut]
    failed: list[GenerateFailure]


class CodingQuestionSummary(BaseModel):
    """List row: no test cases at all."""

    id: UUID
    kind: Literal["coding"] = "coding"
    body: str
    difficulty: Difficulty
    tags: list[str]
    marks: Decimal
    approved: bool
    created_at: datetime

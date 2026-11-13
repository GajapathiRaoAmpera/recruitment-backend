from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.execution.judging import CaseJudgement
from app.execution.languages import LANGUAGES

MAX_CODE_CHARS = 64_000
MAX_OUTPUT_CHARS = 4_000  # a sample's stdout can be as large as the program prints


class CodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: UUID
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    language: str

    @field_validator("language")
    @classmethod
    def _known_language(cls, v: str) -> str:
        if v not in LANGUAGES:
            raise ValueError(f"language must be one of: {', '.join(LANGUAGES)}")
        return v


class CaseResult(BaseModel):
    """One sample case. `stderr` carries whatever explains a failure: compiler output,
    the program's stderr, or "Memory limit exceeded"."""

    case: int  # 1-based, in sample order
    passed: bool
    status: str  # passed | wrong_answer | compile_error | runtime_error | time_limit
    stdout: str
    stderr: str
    time: float | None

    @classmethod
    def of(cls, number: int, j: CaseJudgement) -> "CaseResult":
        return cls(
            case=number,
            passed=j.passed,
            status=j.verdict.value,
            stdout=j.stdout[:MAX_OUTPUT_CHARS],
            stderr=j.detail,
            time=j.time_s,
        )


class RunOut(BaseModel):
    results: list[CaseResult]


class SubmitCodeOut(BaseModel):
    """Counts only. There is deliberately no field that could hold a hidden case's data."""

    passed: int
    total: int

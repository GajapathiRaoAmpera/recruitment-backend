from uuid import UUID

from pydantic import BaseModel, Field


class CandidateRow(BaseModel):
    id: UUID
    student_id: str
    full_name: str
    email: str
    cgpa: float | None
    skills: list[str]
    jd_match: float | None
    parse_status: str  # ok | failed | pending | missing (no resume uploaded)
    shortlisted: bool | None


class CandidatePage(BaseModel):
    items: list[CandidateRow]
    next_cursor: str | None


class FileResult(BaseModel):
    file: str
    status: str  # parsed | parse_failed | unmatched | too_large | storage_failed
    candidate_id: UUID | None = None


class UploadResult(BaseModel):
    results: list[FileResult]


class ShortlistIn(BaseModel):
    candidate_ids: list[UUID] = Field(min_length=1, max_length=1000)
    shortlisted: bool


class ShortlistOut(BaseModel):
    updated: int  # candidates without a resume row are skipped
    moved: int  # invited to Round 1 (shortlist) or taken out of it (un-shortlist)


class ResumeInfo(BaseModel):
    cgpa: float | None
    skills: list[str]
    jd_match: float | None
    parse_status: str
    shortlisted: bool | None
    url: str | None  # API path to the PDF; needs the HR bearer token


class RoundHistory(BaseModel):
    round_id: UUID
    position: int
    type: str
    status: str
    score: float | None
    rank: int | None
    hr_note: str | None


class CandidateProfile(BaseModel):
    id: UUID
    drive_id: UUID
    student_id: str
    full_name: str
    email: str
    phone: str | None
    form_data: dict
    resume: ResumeInfo | None  # None until a resume is uploaded
    rounds: list[RoundHistory]


class ImportError_(BaseModel):
    row: int
    message: str


class ImportResult(BaseModel):
    created: int
    updated: int
    errors: list[ImportError_]


class FetchItem(BaseModel):
    candidate_id: UUID
    student_id: str
    # parsed | parse_failed | no_link | not_accessible | not_pdf | too_large | storage_failed
    status: str


class FetchResult(BaseModel):
    results: list[FetchItem]

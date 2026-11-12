import asyncio
import io
import zipfile
from pathlib import PurePath
from uuid import UUID

from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.candidates import drive as gdrive
from app.candidates import importer, parser, repo, storage
from app.candidates.matcher import match_candidate
from app.candidates.schemas import (
    CandidatePage,
    CandidateProfile,
    FetchItem,
    FetchResult,
    FileResult,
    ImportResult,
    ShortlistIn,
    ShortlistOut,
    UploadResult,
)
from app.core.audit import audit
from app.core.db import get_session
from app.core.errors import AppError

router = APIRouter(tags=["candidates"])
hr = require_role("hr")

MAX_PDF = 5 * 1024 * 1024
MAX_FILES = 500  # per request, zip entries included


async def own_drive(session: AsyncSession, drive_id: UUID, user: StaffUser):
    """HR only reaches their own drives; Super Admin reaches all."""
    owner = None if user.role == "super_admin" else user.id
    drive = await repo.get_drive(session, drive_id, owner)
    if drive is None:
        raise AppError(404, "DRIVE_NOT_FOUND", "Drive not found")
    return drive


async def _pdfs(files: list[UploadFile]):
    """Yield (name, bytes | None). None = skipped as too large. Zips are opened one level."""
    count = 0
    for f in files:
        name = PurePath(f.filename or "").name
        data = (
            await f.read(MAX_PDF * 20 + 1)
            if name.lower().endswith(".zip")
            else await f.read(MAX_PDF + 1)
        )
        if name.lower().endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile as e:
                raise AppError(422, "BAD_ZIP", f"{name} is not a valid zip") from e
            for info in zf.infolist():
                inner = PurePath(info.filename).name
                if info.is_dir() or not inner.lower().endswith(".pdf"):
                    continue
                count += 1
                if count > MAX_FILES:
                    raise AppError(422, "TOO_MANY_FILES", f"At most {MAX_FILES} files per upload")
                yield inner, (zf.read(info) if info.file_size <= MAX_PDF else None)
        elif name.lower().endswith(".pdf"):
            count += 1
            if count > MAX_FILES:
                raise AppError(422, "TOO_MANY_FILES", f"At most {MAX_FILES} files per upload")
            yield name, (data if len(data) <= MAX_PDF else None)


async def _store(session: AsyncSession, drive, cid: UUID, data: bytes, parsed: dict) -> str:
    try:
        path = await storage.upload_pdf(drive["id"], cid, data)
    except AppError:
        return "storage_failed"
    await repo.upsert_resume(session, cid, path, parsed)
    return "parsed" if parsed["parse_status"] == "ok" else "parse_failed"


@router.post("/drives/{drive_id}/resumes", response_model=UploadResult)
async def upload_resumes(
    drive_id: UUID,
    files: list[UploadFile],
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    """PDFs or a zip of PDFs. Each is matched to a candidate by its content (email, phone,
    Student ID, name), not its file name. Unmatched ones are reported, not stored."""
    drive = await own_drive(session, drive_id, user)
    candidates = await repo.drive_candidates(session, drive_id)
    results: list[FileResult] = []
    # shortcut: parsed inline (OCR can take seconds per file); move to BackgroundTasks past ~30 s
    async for name, data in _pdfs(files):
        if data is None:
            results.append(FileResult(file=name, status="too_large"))
            continue
        parsed = await asyncio.to_thread(parser.analyse, data, drive["jd_text"])
        cid = match_candidate(name, parsed["text"], candidates)
        if cid is None:
            results.append(FileResult(file=name, status="unmatched"))
            continue
        status = await _store(session, drive, cid, data, parsed)
        results.append(FileResult(file=name, status=status, candidate_id=cid))
    await audit(session, user.id, "resume.upload", "drive", drive_id, {"files": len(results)})
    await session.commit()
    return {"results": results}


@router.put("/drives/{drive_id}/candidates/{candidate_id}/resume", response_model=FileResult)
async def attach_resume(
    drive_id: UUID,
    candidate_id: UUID,
    file: UploadFile,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    """Manual fallback for a resume the matcher could not place."""
    drive = await own_drive(session, drive_id, user)
    if not await repo.candidate_in_drive(session, drive_id, candidate_id):
        raise AppError(404, "CANDIDATE_NOT_FOUND", "Candidate not found in this drive")
    data = await file.read(MAX_PDF + 1)
    if len(data) > MAX_PDF:
        raise AppError(413, "FILE_TOO_LARGE", "Resume must be 5 MB or smaller")
    parsed = await asyncio.to_thread(parser.analyse, data, drive["jd_text"])
    status = await _store(session, drive, candidate_id, data, parsed)
    await audit(session, user.id, "resume.attach", "candidate", candidate_id)
    await session.commit()
    return FileResult(file=file.filename or "resume.pdf", status=status, candidate_id=candidate_id)


@router.get("/drives/{drive_id}/candidates", response_model=CandidatePage)
async def list_candidates(
    drive_id: UUID,
    skills: str = "",
    min_cgpa: float | None = Query(None, ge=0, le=10),
    min_match: float | None = Query(None, ge=0, le=100),
    shortlisted: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    await own_drive(session, drive_id, user)
    try:
        offset = int(cursor) if cursor else 0
    except ValueError as e:
        raise AppError(422, "BAD_CURSOR", "Invalid cursor") from e
    wanted = [s.strip().lower() for s in skills.split(",") if s.strip()]  # candidate must have all
    rows = await repo.list_candidates(
        session, drive_id, wanted, min_cgpa, min_match, shortlisted, limit, max(offset, 0)
    )
    more = len(rows) > limit
    return {"items": rows[:limit], "next_cursor": str(offset + limit) if more else None}


@router.post("/drives/{drive_id}/shortlist", response_model=ShortlistOut)
async def shortlist(
    drive_id: UUID,
    body: ShortlistIn,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    await own_drive(session, drive_id, user)
    n = await repo.set_shortlisted(session, drive_id, body.candidate_ids, body.shortlisted)
    await audit(
        session,
        user.id,
        "candidate.shortlist",
        "drive",
        drive_id,
        {"shortlisted": body.shortlisted, "count": n},
    )
    await session.commit()
    return {"updated": n}


@router.get("/candidates/{candidate_id}", response_model=CandidateProfile)
async def get_candidate(
    candidate_id: UUID,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    owner = None if user.role == "super_admin" else user.id
    row = await repo.get_profile(session, candidate_id, owner)
    if row is None:  # also covers another HR's candidate: don't reveal it exists
        raise AppError(404, "CANDIDATE_NOT_FOUND", "Candidate not found")
    resume = None
    if row["storage_path"]:
        resume = {**row, "url": f"/api/v1/candidates/{candidate_id}/resume"}
    return {**row, "resume": resume}


@router.post("/drives/{drive_id}/candidates/import", response_model=ImportResult)
async def import_candidates(
    drive_id: UUID,
    sheet: UploadFile,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    """Google Form export (.xlsx/.csv). Re-importing updates by Student ID, so a student who
    submitted the form twice ends up with their latest row."""
    await own_drive(session, drive_id, user)
    data = await sheet.read(MAX_PDF + 1)
    if len(data) > MAX_PDF:
        raise AppError(413, "FILE_TOO_LARGE", "Sheet must be 5 MB or smaller")
    rows, errors = await asyncio.to_thread(importer.parse_sheet, sheet.filename or "", data)
    created = 0
    for row in rows:
        created += await repo.upsert_candidate(session, drive_id, row)
    await audit(
        session,
        user.id,
        "candidate.import",
        "drive",
        drive_id,
        {"created": created, "updated": len(rows) - created, "errors": len(errors)},
    )
    await session.commit()
    return {"created": created, "updated": len(rows) - created, "errors": errors}


async def _fetch_one(c: dict, drive_id: UUID, jd_text: str, gate: asyncio.Semaphore):
    """Download + parse + save the file. No DB access, so many can run at once."""
    link = gdrive.resume_link(c["form_data"])
    fid = gdrive.file_id(link) if link else None
    if fid is None:
        return c, "no_link", None, None, None
    async with gate:
        try:
            data = await gdrive.fetch_pdf(fid)
        except gdrive.DriveError as e:
            return c, e.code, None, None, None
        parsed = await asyncio.to_thread(parser.analyse, data, jd_text)
        try:
            path = await storage.upload_pdf(drive_id, c["id"], data)
        except AppError:
            return c, "storage_failed", None, None, None
    return c, "parsed" if parsed["parse_status"] == "ok" else "parse_failed", path, parsed, data


@router.post("/drives/{drive_id}/resumes/fetch", response_model=FetchResult)
async def fetch_resumes(
    drive_id: UUID,
    refetch: bool = False,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    """Download each candidate's resume from the Drive link in their form answers.
    The file must be shared as "Anyone with the link"; private files come back `not_accessible`.
    By default only candidates without a resume are fetched; `refetch=true` redoes everyone."""
    drive = await own_drive(session, drive_id, user)
    todo = await repo.candidates_to_fetch(session, drive_id, refetch)
    gate = asyncio.Semaphore(5)
    # shortcut: one request does the whole drive (about a minute per 100 resumes);
    # move to BackgroundTasks with a progress endpoint if a drive passes ~500 candidates
    done = await asyncio.gather(*(_fetch_one(c, drive_id, drive["jd_text"], gate) for c in todo))
    results = []
    for c, status, path, parsed, _ in done:
        if path is not None:
            await repo.upsert_resume(session, c["id"], path, parsed)
        results.append(FetchItem(candidate_id=c["id"], student_id=c["student_id"], status=status))
    await audit(session, user.id, "resume.fetch", "drive", drive_id, {"candidates": len(results)})
    await session.commit()
    return {"results": results}


@router.get("/candidates/{candidate_id}/resume")
async def download_resume(
    candidate_id: UUID,
    user: StaffUser = Depends(hr),
    session: AsyncSession = Depends(get_session),
):
    """The resume PDF. Behind the HR login (not a public folder), so the UI has to fetch it
    with the bearer token and show it as a blob."""
    owner = None if user.role == "super_admin" else user.id
    row = await repo.get_profile(session, candidate_id, owner)
    path = storage.file_path(row["storage_path"]) if row and row["storage_path"] else None
    if path is None:
        raise AppError(404, "RESUME_NOT_FOUND", "No resume file for this candidate")
    return FileResponse(path, media_type="application/pdf", filename=f"{row['student_id']}.pdf")

"""Resume PDFs on the server's disk, under RESUME_DIR. Keys are built from UUIDs only.

shortcut: local disk is lost if the server runs in a container without a mounted volume, and
is not shared between several servers; mount a volume (or move back to object storage) before prod.
"""

import asyncio
from pathlib import Path
from uuid import UUID

from app.core.config import get_settings
from app.core.errors import AppError


def _full(key: str) -> Path:
    base = Path(get_settings().resume_dir).resolve()
    path = (base / key).resolve()
    if base not in path.parents:  # never leave the resume folder
        raise AppError(400, "BAD_PATH", "Invalid file path")
    return path


def _write(key: str, data: bytes) -> None:
    path = _full(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def upload_pdf(drive_id: UUID, candidate_id: UUID, data: bytes) -> str:
    """Save the PDF (re-uploads overwrite) and return its key, stored in resumes.storage_path."""
    key = f"{drive_id}/{candidate_id}.pdf"
    try:
        await asyncio.to_thread(_write, key, data)
    except OSError as e:
        raise AppError(500, "STORAGE_ERROR", "Couldn't save the resume file") from e
    return key


def file_path(key: str) -> Path | None:
    """The file on disk, or None if it is missing."""
    path = _full(key)
    return path if path.is_file() else None

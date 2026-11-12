"""Question-bank PDFs on the server's disk, under QBANK_DIR, as `<key>.pdf`. Keys are UUIDs only.

shortcut: local disk is lost if the server runs in a container without a mounted volume, and
is not shared between several servers; mount a volume (or move to object storage) before prod.
Mirrors app/candidates/storage.py.
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from app.core.config import get_settings
from app.core.errors import AppError


def new_key() -> str:
    """A fresh storage key (canonical lowercase UUID string), stored in question_sources."""
    return str(uuid4())


def _full(key: str) -> Path:
    try:
        ok = str(UUID(key)) == key  # canonical form only: no separators, dots or drive letters
    except (ValueError, TypeError, AttributeError):
        ok = False
    base = Path(get_settings().qbank_dir).resolve()
    path = (base / f"{key}.pdf").resolve()
    if not ok or path.parent != base:  # never leave the bank folder
        raise AppError(400, "BAD_PATH", "Invalid file path")
    return path


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)  # a replace never leaves a half-written file under the key
    except BaseException:
        tmp.unlink(missing_ok=True)  # no half-written .tmp left behind
        raise


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


async def save(key: str, data: bytes) -> None:
    """Write (or overwrite) the file for `key`. Raises AppError BAD_PATH / STORAGE_ERROR."""
    path = _full(key)
    try:
        await asyncio.to_thread(_write, path, data)
    except OSError as e:
        raise AppError(500, "STORAGE_ERROR", "Couldn't save the question-bank file") from e


async def read(key: str) -> bytes | None:
    """The file's bytes, or None when it is missing. Raises AppError BAD_PATH."""
    path = _full(key)
    try:
        return await asyncio.to_thread(_read, path)
    except OSError as e:
        raise AppError(500, "STORAGE_ERROR", "Couldn't read the question-bank file") from e


async def delete(key: str) -> None:
    """Remove the file; a missing file is fine. Raises AppError BAD_PATH."""
    path = _full(key)
    try:
        await asyncio.to_thread(path.unlink, True)
    except OSError as e:
        raise AppError(500, "STORAGE_ERROR", "Couldn't delete the question-bank file") from e

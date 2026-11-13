"""Latest camera still per live interview, held in this process's memory only.

Nothing reaches disk or the database, and a frame is served only while it is fresh. One API process
(D2) makes this work; it would not survive several workers or a restart (accepted, docs/09).
"""

import time
from dataclasses import dataclass
from uuid import UUID

MAX_BYTES = 100 * 1024
FRESH_SECONDS = 10.0
MAX_ENTRIES = 2000  # guard against unbounded growth; expired entries are pruned on every write

_JPEG_MAGIC = b"\xff\xd8\xff"


@dataclass(frozen=True)
class Frame:
    data: bytes
    at: float  # time.monotonic()


_frames: dict[UUID, Frame] = {}


def is_jpeg(data: bytes) -> bool:
    return data.startswith(_JPEG_MAGIC)


def _prune(now: float) -> None:
    for key in [k for k, f in _frames.items() if now - f.at > FRESH_SECONDS]:
        _frames.pop(key, None)


def put(interview_id: UUID, data: bytes, *, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    _prune(now)
    if len(_frames) >= MAX_ENTRIES and interview_id not in _frames:
        oldest = min(_frames, key=lambda k: _frames[k].at)
        _frames.pop(oldest, None)
    _frames[interview_id] = Frame(data, now)


def get(interview_id: UUID, *, now: float | None = None) -> bytes | None:
    now = time.monotonic() if now is None else now
    f = _frames.get(interview_id)
    if f is None or now - f.at > FRESH_SECONDS:
        return None
    return f.data


def drop(interview_id: UUID) -> None:
    _frames.pop(interview_id, None)


def fresh_ids(*, now: float | None = None) -> set[UUID]:
    now = time.monotonic() if now is None else now
    return {k for k, f in _frames.items() if now - f.at <= FRESH_SECONDS}


def clear() -> None:  # tests
    _frames.clear()

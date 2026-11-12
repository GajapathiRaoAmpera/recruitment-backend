"""Question-bank file storage (local disk shortcut); keys are UUIDs only."""

import asyncio
import uuid

import pytest

from app.core.config import get_settings
from app.core.errors import AppError
from app.qbank import storage

PDF = b"%PDF-1.7\n%fake\n"


@pytest.fixture(autouse=True)
def _qbank_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "qbank_dir", str(tmp_path))


def test_save_read_delete_roundtrip(tmp_path):
    key = str(uuid.uuid4())
    asyncio.run(storage.save(key, PDF))
    assert (tmp_path / f"{key}.pdf").read_bytes() == PDF
    assert asyncio.run(storage.read(key)) == PDF
    asyncio.run(storage.save(key, PDF + b"v2"))  # replace overwrites
    assert asyncio.run(storage.read(key)) == PDF + b"v2"
    asyncio.run(storage.delete(key))
    assert asyncio.run(storage.read(key)) is None
    asyncio.run(storage.delete(key))  # deleting a missing file is a no-op


@pytest.mark.parametrize(
    "key",
    [
        "../../etc/passwd",
        "..",
        "",
        "a/b",
        "x" * 36,
        str(uuid.uuid4()) + "/../x",
        str(uuid.uuid4()).upper(),  # only the canonical lowercase form
        "C:\\Windows\\system32",
    ],
)
def test_rejects_traversal_keys(key):
    with pytest.raises(AppError) as e:
        asyncio.run(storage.save(key, PDF))
    assert e.value.code == "BAD_PATH"
    with pytest.raises(AppError):
        asyncio.run(storage.read(key))
    with pytest.raises(AppError):
        asyncio.run(storage.delete(key))


def test_read_missing_returns_none():
    assert asyncio.run(storage.read(str(uuid.uuid4()))) is None


def test_failed_write_removes_tmp_file(tmp_path, monkeypatch):
    key = str(uuid.uuid4())

    def fail_replace(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(storage.Path, "replace", fail_replace)
    with pytest.raises(AppError) as e:
        asyncio.run(storage.save(key, PDF))
    assert e.value.code == "STORAGE_ERROR"
    assert list(tmp_path.iterdir()) == []  # neither the .tmp nor the target is left


def test_new_key_is_a_valid_uuid():
    key = storage.new_key()
    assert str(uuid.UUID(key)) == key

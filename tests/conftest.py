import os

# Dummy config for tests; real env vars still win if set.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-" + "0123456789abcdef" * 4)

import pytest  # noqa: E402

from app.notifications import round_invites  # noqa: E402


@pytest.fixture(autouse=True)
def no_candidate_mail(monkeypatch):
    """API tests use fake sessions: record who would be emailed instead of querying/sending."""
    calls = []

    async def for_round(_, round_id, ids, shortlisted):
        calls.append((round_id, list(ids), shortlisted))
        return []

    monkeypatch.setattr(round_invites, "for_round", for_round)
    return calls

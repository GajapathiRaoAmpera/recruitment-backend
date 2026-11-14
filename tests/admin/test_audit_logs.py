"""FR-27 / AC-10: Super Admin reads the audit log; HR cannot."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.admin import repo
from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.main import app

ADMIN = StaffUser(id=uuid.uuid4(), email="sa@x.com", full_name="S A", role="super_admin")
HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
NOW = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)


def row(i, **over):
    return {
        "id": i,
        "created_at": NOW,
        "actor_id": HR.id,
        "actor_name": "H R",
        "actor_email": "hr@x.com",
        "actor_role": "hr",
        "action": "round.advance",
        "entity": "round",
        "entity_id": str(uuid.uuid4()),
        "diff": {"threshold": 50},
        **over,
    }


class Captured:
    """Records the arguments of the last list_audit_logs call and replays canned rows."""

    def __init__(self):
        self.calls, self.counts, self.rows = [], [], []


class SqlSession:
    """Captures the SQL and parameters the real repo sends."""

    def __init__(self):
        self.sql, self.params = "", {}

    async def execute(self, stmt, params=None):
        self.sql, self.params = str(stmt), params or {}

        class R:
            def mappings(self):
                return []

            def scalars(self):
                return iter(["round.advance", "user.create"])

            def scalar_one(self):
                return 7

        return R()


@pytest.fixture
def db(monkeypatch):
    cap = Captured()

    async def list_audit_logs(_, **kw):
        cap.calls.append(kw)
        return cap.rows[: kw["limit"] + 1]

    async def count_audit_logs(_, **kw):
        cap.counts.append(kw)
        return len(cap.rows)

    async def list_audit_actions(_):
        return ["round.advance", "user.create"]

    async def audit_summary(_):
        return {"total": 12, "last_24h": 3, "actors": 2, "access_changes": 1}

    monkeypatch.setattr(repo, "list_audit_logs", list_audit_logs)
    monkeypatch.setattr(repo, "count_audit_logs", count_audit_logs)
    monkeypatch.setattr(repo, "list_audit_actions", list_audit_actions)
    monkeypatch.setattr(repo, "audit_summary", audit_summary)
    app.dependency_overrides[get_session] = lambda: object()
    yield cap
    app.dependency_overrides.clear()


def as_user(user):
    app.dependency_overrides[security.current_user] = lambda: user
    return TestClient(app)


PATHS = [
    "/api/v1/admin/audit-logs",
    "/api/v1/admin/audit-logs/actions",
    "/api/v1/admin/audit-logs/summary",
]


@pytest.mark.parametrize("path", PATHS)
def test_hr_forbidden(db, path):
    r = as_user(HR).get(path)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    assert db.calls == []


@pytest.mark.parametrize("path", PATHS)
def test_needs_login(db, path):
    assert TestClient(app).get(path).status_code == 401


def test_list_returns_rows_with_actor_and_diff(db):
    db.rows = [row(3), row(2, actor_id=None, actor_name=None, actor_email=None, diff=None)]
    r = as_user(ADMIN).get("/api/v1/admin/audit-logs")
    assert r.status_code == 200
    body = r.json()
    assert body["next_cursor"] is None and body["total"] == 2
    first, second = body["items"]
    assert first["id"] == 3 and first["actor_name"] == "H R" and first["diff"] == {"threshold": 50}
    assert first["actor_role"] == "hr"
    assert second["actor_id"] is None and second["diff"] is None
    assert db.calls[0] == dict(
        actor_id=None, action=None, q=None, since=None, until=None, before_id=None, limit=50
    )


def test_paging_uses_last_id_as_cursor(db):
    db.rows = [row(i) for i in (9, 8, 7)]
    r = as_user(ADMIN).get("/api/v1/admin/audit-logs", params={"limit": 2})
    assert [i["id"] for i in r.json()["items"]] == [9, 8]
    assert r.json()["next_cursor"] == "8"

    as_user(ADMIN).get("/api/v1/admin/audit-logs", params={"limit": 2, "cursor": "8"})
    assert db.calls[1]["before_id"] == 8


def test_filters_are_passed_through(db):
    params = {
        "actor_id": str(HR.id),
        "action": "user.update",
        "q": "  priya  ",
        "since": "2026-11-01T00:00:00+05:30",
        "until": "2026-11-15T00:00:00+05:30",
    }
    r = as_user(ADMIN).get("/api/v1/admin/audit-logs", params=params)
    assert r.status_code == 200
    call = db.calls[0]
    assert call["actor_id"] == HR.id and call["action"] == "user.update" and call["q"] == "priya"
    assert call["since"].isoformat() == "2026-11-01T00:00:00+05:30"
    assert call["until"].isoformat() == "2026-11-15T00:00:00+05:30"
    # the total counts the same filters (without the page cursor)
    assert db.counts[0] == {k: v for k, v in call.items() if k not in ("before_id", "limit")}


def test_blank_search_is_ignored(db):
    as_user(ADMIN).get("/api/v1/admin/audit-logs", params={"q": "   ", "action": ""})
    assert db.calls[0]["q"] is None and db.calls[0]["action"] is None


@pytest.mark.parametrize(
    "params",
    [{"cursor": "abc"}, {"limit": 0}, {"limit": 201}, {"actor_id": "nope"}, {"since": "x"}],
)
def test_bad_query_is_422(db, params):
    r = as_user(ADMIN).get("/api/v1/admin/audit-logs", params=params)
    assert r.status_code == 422 and db.calls == []


def test_summary(db):
    r = as_user(ADMIN).get("/api/v1/admin/audit-logs/summary")
    assert r.status_code == 200
    assert r.json() == {"total": 12, "last_24h": 3, "actors": 2, "access_changes": 1}


def test_actions_list(db):
    r = as_user(ADMIN).get("/api/v1/admin/audit-logs/actions")
    assert r.status_code == 200 and r.json() == ["round.advance", "user.create"]


# The repo itself: SQL shape and parameter binding (no database needed).


def test_repo_binds_every_filter_and_escapes_search():
    s = SqlSession()
    asyncio.run(
        repo.list_audit_logs(
            s,
            actor_id=HR.id,
            action="user.update",
            q="50%_a\\b",
            since=NOW,
            until=NOW,
            before_id=10,
            limit=20,
        )
    )
    assert "left join users u on u.id = a.actor_id" in s.sql
    assert "order by a.id desc limit :limit" in s.sql
    for frag in ("a.actor_id = :actor_id", "a.action = :action", "a.created_at >= :since",
                 "a.created_at < :until", "a.id < :before_id", "u.email ilike :q"):  # fmt: skip
        assert frag in s.sql
    assert s.params["q"] == "%50\\%\\_a\\\\b%"
    assert s.params["limit"] == 21 and s.params["before_id"] == 10


def test_repo_without_filters_has_no_where():
    s = SqlSession()
    asyncio.run(
        repo.list_audit_logs(
            s, actor_id=None, action=None, q=None, since=None, until=None, before_id=None, limit=50
        )
    )
    assert "where" not in s.sql and s.params == {"limit": 51}


def test_repo_count_uses_the_same_filters_without_paging():
    s = SqlSession()
    n = asyncio.run(
        repo.count_audit_logs(s, actor_id=None, action="user.update", q="x", since=None, until=None)
    )
    assert n == 7 and s.sql.startswith("select count(*) from audit_logs a left join users u")
    assert "a.action = :action" in s.sql and "u.email ilike :q" in s.sql
    assert "limit" not in s.sql and "before_id" not in s.params


def test_repo_list_selects_actor_role():
    s = SqlSession()
    asyncio.run(
        repo.list_audit_logs(
            s, actor_id=None, action=None, q=None, since=None, until=None, before_id=None, limit=5
        )
    )
    assert "cast(u.role as text) as actor_role" in s.sql

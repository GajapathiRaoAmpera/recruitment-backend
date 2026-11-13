"""The paper repo's SQL guards (no DB: a recording session captures each statement)."""

import asyncio
import json
import uuid

from app.papers import repo


class Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def one(self):
        assert len(self._rows) == 1
        return self._rows[0]

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class RecordingSession:
    def __init__(self, *results):
        self.calls: list[tuple[str, dict]] = []
        self._results = list(results)

    async def execute(self, stmt, params=None):
        self.calls.append((" ".join(str(stmt).split()), params or {}))
        return Result(self._results.pop(0) if self._results else [])


def run(coro):
    return asyncio.run(coro)


POOL = [
    {
        "id": str(uuid.uuid4()),
        "level": "medium",
        "section": "technical",
        "topic": "SQL",
        "style": "syntax",
        "text": "Which keyword removes duplicates?",
        "options": ["DISTINCT", "UNIQUE", "ONLY", "SINGLE"],
        "answer": 0,
        "why": "SELECT DISTINCT removes duplicate rows.",
    }
]


def test_persist_questions_uses_on_conflict_do_nothing():
    did = uuid.uuid4()
    s = RecordingSession([{"id": POOL[0]["id"]}], [])
    assert run(repo.persist_questions(s, did, POOL)) == 1
    insert_sql, params = s.calls[0]
    assert insert_sql.startswith("insert into questions")
    assert "on conflict (id) do nothing" in insert_sql
    assert (
        "cast('mcq' as question_kind)" in insert_sql and "true, true" in insert_sql
    )  # ai_generated, approved
    assert "'paper:' || d.id::text" in insert_sql and "'track:' || d.track" in insert_sql
    assert "'section:' || r.section" in insert_sql and "'style:' || r.style" in insert_sql
    assert "explanation" in insert_sql
    item = json.loads(params["items"])[0]
    assert item["id"] == POOL[0]["id"] and item["answer"] == 0
    assert item["difficulty"] == "medium" and item["explanation"].startswith("SELECT DISTINCT")
    stamp_sql, _ = s.calls[1]
    assert "set persisted_at = now()" in stamp_sql and "persisted_at is null" in stamp_sql


def test_persist_questions_empty_pool_only_stamps():
    s = RecordingSession()
    assert run(repo.persist_questions(s, uuid.uuid4(), [])) == 0
    assert len(s.calls) == 1 and "persisted_at" in s.calls[0][0]


def test_mark_accepted_only_from_draft():
    s = RecordingSession([])
    assert run(repo.mark_accepted(s, uuid.uuid4())) is None
    sql, _ = s.calls[0]
    assert "set status = 'accepted', accepted_at = now()" in sql
    assert "where id = :id and status = 'draft'" in sql


def test_save_pool_only_when_draft():
    s = RecordingSession([], [{"id": 1}])
    did = uuid.uuid4()
    assert run(repo.save_pool_and_checks(s, did, POOL, [])) is False
    assert run(repo.save_pool_and_checks(s, did, POOL, [])) is True
    sql, params = s.calls[0]
    assert "where id = :id and status = 'draft'" in sql
    assert json.loads(params["pool"]) == POOL


def test_insert_issues_on_conflict_do_nothing():
    s = RecordingSession([{"candidate_ref": "d1-c0"}])
    rows = [{"candidate_ref": "d1-c0", "seed": "s0"}, {"candidate_ref": "d1-c1", "seed": "s1"}]
    assert run(repo.insert_issues(s, uuid.uuid4(), rows)) == 1
    sql, params = s.calls[0]
    assert "on conflict (draft_id, candidate_ref) do nothing" in sql
    assert json.loads(params["rows"]) == rows


def test_generation_updates_only_while_generating():
    did = uuid.uuid4()
    s = RecordingSession([], [], [])
    run(repo.update_progress(s, did, 3, 10))
    run(repo.finish_generation(s, did, POOL, []))
    run(repo.fail_generation(s, did, "boom"))
    assert all("status = 'generating'" in sql.split("where", 1)[1] for sql, _ in s.calls)


def test_list_drafts_filters_owner_for_hr_only():
    owner = uuid.uuid4()
    s = RecordingSession([], [])
    run(repo.list_drafts(s, owner))
    run(repo.list_drafts(s, None))
    assert "where owner_id = :owner" in s.calls[0][0] and s.calls[0][1] == {"owner": owner}
    assert "where" not in s.calls[1][0]
    assert "pool," not in s.calls[0][0]  # the list never ships the pool


def test_get_draft_for_update_locks_row():
    did = uuid.uuid4()
    s = RecordingSession([], [])
    run(repo.get_draft(s, did, for_update=True))
    run(repo.get_draft(s, did))
    assert s.calls[0][0].endswith("where id = :id for update")
    assert "for update" not in s.calls[1][0]


def test_finish_generation_ignores_timed_out_draft():
    s = RecordingSession([])
    assert run(repo.finish_generation(s, uuid.uuid4(), POOL, [])) is False
    sql, params = s.calls[0]
    assert "created_at > now() - make_interval(mins => :stale_minutes)" in sql
    assert params["stale_minutes"] == repo.STALE_MINUTES == 15

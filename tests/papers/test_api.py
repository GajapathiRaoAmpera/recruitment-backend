"""Paper builder API: create + background generation, review, edit, accept, send (no DB/network)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.core.errors import AppError
from app.main import app
from app.papers import builder, repo, service
from app.papers.schemas import LEVELS, SECTIONS, PaperSettings, PoolQuestion

ADMIN = StaffUser(id=uuid.uuid4(), email="sa@x.com", full_name="S A", role="super_admin")
HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
HR2 = StaffUser(id=uuid.uuid4(), email="hr2@x.com", full_name="H R2", role="hr")

SETTINGS = {
    "track": "it",
    "counts": {"easy": 4, "medium": 3, "advanced": 3},
    "mix": {"aptitude": 40, "reasoning": 30, "technical": 30},
    "pool_factor": 2,
}
BASE = "/api/v1/papers"


def make_pool(settings: PaperSettings, factor: int | None = None) -> list[PoolQuestion]:
    factor = settings.pool_factor if factor is None else factor
    want = builder.needed(settings)
    pool = []
    for level in LEVELS:
        for section in SECTIONS:
            for i in range(want[level][section] * factor):
                tag = f"{level}-{section}-{i}"
                pool.append(
                    PoolQuestion(
                        id=str(uuid.uuid4()),
                        level=level,
                        section=section,
                        topic="t",
                        style="concept" if section == "technical" else section,
                        text=f"Question {tag}?",
                        options=[f"{tag} A", f"{tag} B", f"{tag} C", f"{tag} D"],
                        answer=i % 4,
                        why=f"Because {tag}.",
                    )
                )
    return pool


class FakeSession:
    def __init__(self):
        self.commits = self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def db(monkeypatch):
    drafts: dict[uuid.UUID, dict] = {}
    issues: dict[uuid.UUID, dict[str, dict]] = {}
    questions: dict[str, dict] = {}
    audits: list[tuple] = []
    progress_log: list[tuple[int, int]] = []
    reads: list[tuple[uuid.UUID, bool]] = []  # (draft_id, for_update)
    session = FakeSession()
    bg_session = FakeSession()

    def summary(d):
        return {k: v for k, v in d.items() if k != "pool"} | {"pool_size": len(d["pool"])}

    async def insert_draft(_, *, drive_ref, owner_id, title, track, settings):
        did = uuid.uuid4()
        drafts[did] = dict(
            id=did,
            drive_ref=drive_ref,
            owner_id=owner_id,
            title=title,
            track=track,
            settings=settings,
            pool=[],
            checks=[],
            status="generating",
            progress={"done": 0, "total": 0},
            error=None,
            created_at=datetime.now(UTC),
            accepted_at=None,
            persisted_at=None,
        )
        return summary(drafts[did])

    async def get_draft(_, did, *, for_update=False):
        reads.append((did, for_update))
        d = drafts.get(did)
        return None if d is None else summary(d) | {"pool": d["pool"]}

    async def list_drafts(_, owner_id):
        return [summary(d) for d in drafts.values() if owner_id in (None, d["owner_id"])]

    async def update_progress(_, did, done, total):
        progress_log.append((done, total))
        if drafts[did]["status"] == "generating":
            drafts[did]["progress"] = {"done": done, "total": total}

    async def finish_generation(_, did, pool, checks):
        # mirrors the SQL guard: status 'generating' and not older than STALE_MINUTES
        age = datetime.now(UTC) - drafts[did]["created_at"]
        if drafts[did]["status"] != "generating" or age > timedelta(minutes=repo.STALE_MINUTES):
            return False
        drafts[did].update(pool=pool, checks=checks, status="draft", error=None)
        return True

    async def fail_generation(_, did, error):
        if drafts[did]["status"] != "generating":
            return False
        drafts[did].update(status="failed", error=error)
        return True

    async def save_pool_and_checks(_, did, pool, checks):
        if drafts[did]["status"] != "draft":
            return False
        drafts[did].update(pool=pool, checks=checks)
        return True

    async def mark_accepted(_, did):
        if drafts[did]["status"] != "draft":
            return None
        drafts[did].update(status="accepted", accepted_at=datetime.now(UTC))
        return summary(drafts[did])

    async def persist_questions(_, did, pool):
        new = [q for q in pool if q["id"] not in questions]
        for q in new:
            questions[q["id"]] = q
        drafts[did]["persisted_at"] = drafts[did]["persisted_at"] or datetime.now(UTC)
        return len(new)

    async def insert_issues(_, did, rows):
        mine = issues.setdefault(did, {})
        new = [r for r in rows if r["candidate_ref"] not in mine]
        for r in new:
            mine[r["candidate_ref"]] = {**r, "issued_at": datetime.now(UTC)}
        return len(new)

    async def list_issues(_, did):
        return list(issues.get(did, {}).values())

    async def get_prompt_overrides(_):
        return {}

    async def audit(_, actor_id, action, entity, entity_id, diff=None):
        audits.append((actor_id, action, entity_id, diff))

    for name, fn in dict(
        insert_draft=insert_draft,
        get_draft=get_draft,
        list_drafts=list_drafts,
        update_progress=update_progress,
        finish_generation=finish_generation,
        fail_generation=fail_generation,
        save_pool_and_checks=save_pool_and_checks,
        mark_accepted=mark_accepted,
        persist_questions=persist_questions,
        insert_issues=insert_issues,
        list_issues=list_issues,
        get_prompt_overrides=get_prompt_overrides,
    ).items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(service, "audit", audit)
    monkeypatch.setattr(service, "open_session", lambda: bg_session)
    monkeypatch.setattr(service, "ai_status", lambda: {"nvidia": True})

    async def fake_pool(settings, *, on_progress, overrides, **_):
        await on_progress(0, 4)
        await on_progress(4, 4)
        return make_pool(settings)

    monkeypatch.setattr(service, "build_pool", fake_pool)
    app.dependency_overrides[get_session] = lambda: session
    yield dict(
        drafts=drafts,
        issues=issues,
        questions=questions,
        audits=audits,
        progress=progress_log,
        reads=reads,
        session=session,
        bg_session=bg_session,
    )
    app.dependency_overrides.clear()


def as_user(user):
    app.dependency_overrides[security.current_user] = lambda: user
    return TestClient(app)


def seed_draft(db, owner=HR, status="draft", pool=None, **kw):
    settings = PaperSettings.model_validate(SETTINGS)
    pool = make_pool(settings) if pool is None else pool
    did = uuid.uuid4()
    db["drafts"][did] = (
        dict(
            id=did,
            drive_ref="d1",
            owner_id=owner.id,
            title="Round 1",
            track="it",
            settings=settings.model_dump(),
            pool=[q.model_dump() for q in pool],
            checks=[c.model_dump() for c in builder.run_checks(settings, "d1", pool)],
            status=status,
            progress={"done": 10, "total": 10},
            error=None,
            created_at=datetime.now(UTC),
            accepted_at=None,
            persisted_at=None,
        )
        | kw
    )
    return did


def create(client, **body):
    return client.post(
        f"{BASE}/drafts", json={"drive_ref": "d1", "title": "Round 1", "settings": SETTINGS} | body
    )


def actions(db):
    return [a[1] for a in db["audits"]]


# --- create + generation ---


def test_create_starts_generation_and_audits(db, monkeypatch):
    scheduled = []

    async def fake_run(draft_id, actor_id):
        scheduled.append((draft_id, actor_id))

    monkeypatch.setattr(service, "run_generation", fake_run)
    r = create(as_user(HR))
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "generating" and body["owner_id"] == str(HR.id)
    assert body["progress"] == {"done": 0, "total": 0} and body["pool_size"] == 0
    assert "pool" not in body
    assert scheduled == [(uuid.UUID(body["id"]), HR.id)]
    assert db["audits"][0][:2] == (HR.id, "paper_draft.create")
    assert db["session"].commits == 1


@pytest.mark.parametrize("track", ["it", "non_it"])
def test_create_503_when_key_missing(db, monkeypatch, track):
    monkeypatch.setattr(service, "ai_status", lambda: {"nvidia": False})
    r = create(as_user(HR), settings=SETTINGS | {"track": track})
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "AI_PROVIDER_NOT_CONFIGURED" and err["details"] == {
        "providers": ["nvidia"]
    }
    assert "NVIDIA API key" in err["message"]
    assert db["drafts"] == {} and db["audits"] == []


def test_ai_status_reports_booleans_only(db, monkeypatch):
    monkeypatch.setattr(service, "ai_status", lambda: {"nvidia": True})
    r = as_user(HR).get(f"{BASE}/ai-status")
    assert r.status_code == 200 and r.json() == {"nvidia": True}


def test_generation_success_sets_draft_with_checks(db):
    r = create(as_user(HR))  # TestClient runs the background task before returning
    did = uuid.UUID(r.json()["id"])
    d = db["drafts"][did]
    assert d["status"] == "draft" and d["error"] is None
    assert d["progress"] == {"done": 4, "total": 4} and db["progress"] == [(0, 4), (4, 4)]
    assert len(d["pool"]) == 20
    assert {c["id"] for c in d["checks"]} >= {"pool_covers", "no_repeats", "answers_present"}
    assert all(c["ok"] for c in d["checks"])
    assert "paper_draft.generated" in actions(db)
    detail = as_user(HR).get(f"{BASE}/drafts/{did}").json()
    assert detail["status"] == "draft" and detail["pool_size"] == 20


@pytest.mark.parametrize(
    "exc,message",
    [
        (
            AppError(422, "POOL_TOO_SMALL", "Too few generated questions passed verification."),
            "Too few generated questions passed verification.",
        ),
        (RuntimeError("https://api.example/key=SECRET boom"), service.GENERIC_FAILURE),
    ],
)
def test_generation_failure_sets_failed_with_message(db, monkeypatch, exc, message):
    async def boom(settings, *, on_progress, overrides, **_):
        await on_progress(0, 4)
        raise exc

    monkeypatch.setattr(service, "build_pool", boom)
    did = uuid.UUID(create(as_user(HR)).json()["id"])
    d = db["drafts"][did]
    assert d["status"] == "failed" and d["error"] == message and d["pool"] == []
    assert "SECRET" not in str(db["audits"])
    assert actions(db)[-1] == "paper_draft.generation_failed"
    detail = as_user(HR).get(f"{BASE}/drafts/{did}").json()
    assert detail["status"] == "failed" and detail["error"] == message


def test_stale_generating_reported_failed(db):
    old = seed_draft(
        db, status="generating", pool=[], created_at=datetime.now(UTC) - timedelta(minutes=16)
    )
    fresh = seed_draft(db, status="generating", pool=[])
    c = as_user(HR)
    r = c.get(f"{BASE}/drafts/{old}").json()
    assert r["status"] == "failed" and r["error"] == "Generation timed out"
    assert c.get(f"{BASE}/drafts/{fresh}").json()["status"] == "generating"
    listed = {d["id"]: d["status"] for d in c.get(f"{BASE}/drafts").json()}
    assert listed == {str(old): "failed", str(fresh): "generating"}


# --- review ---


def test_detail_has_one_sample_paper(db):
    did = seed_draft(db)
    r = as_user(HR).get(f"{BASE}/drafts/{did}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["sample"]) == 10  # easy 4 + medium 3 + advanced 3
    assert {"answer", "why"} <= set(body["sample"][0])
    assert len({q["id"] for q in body["sample"]}) == 10
    assert body["overlap"]["total"] == 10 and body["overlap"]["shared"] < 10
    assert body["pool_size"] == 20 and body["edited_count"] == 0 and body["issues"] == []
    assert all(c["ok"] for c in body["checks"])
    assert len(body["pool"]) == 20 and all(q["selected"] for q in body["pool"])
    assert body["selected_count"] == 20


# --- edit ---


def test_edit_question_updates_pool_and_flags_edited(db):
    did = seed_draft(db)
    c = as_user(HR)
    qid = c.get(f"{BASE}/drafts/{did}").json()["sample"][0]["id"]
    edit = {"text": " New text? ", "options": ["w", "x", "y", "z"], "answer": 2, "why": "Since."}
    r = c.patch(f"{BASE}/drafts/{did}/questions/{qid}", json=edit)
    assert r.status_code == 200
    body = r.json()
    assert body["edited_count"] == 1
    item = next(q for q in body["sample"] if q["id"] == qid)
    assert item["text"] == "New text?" and item["edited"] is True
    assert item["options"][item["answer"]] == "y"  # answer follows the option shuffle
    stored = next(q for q in db["drafts"][did]["pool"] if q["id"] == qid)
    assert stored["text"] == "New text?" and stored["options"] == ["w", "x", "y", "z"]
    assert stored["answer"] == 2 and stored["edited"] is True
    assert db["session"].commits == 1


@pytest.mark.parametrize(
    "edit",
    [
        {"text": "  ", "options": ["a", "b", "c", "d"], "answer": 0, "why": "w"},
        {"text": "T", "options": ["a", "A", "c", "d"], "answer": 0, "why": "w"},
        {"text": "T", "options": ["a", "b", "c"], "answer": 0, "why": "w"},
        {"text": "T", "options": ["a", "b", "c", "d"], "answer": 4, "why": "w"},
        {"text": "T", "options": ["a", "b", "c", "d"], "answer": 0, "why": " "},
    ],
)
def test_edit_rejects_invalid_and_is_audited(db, edit):
    did = seed_draft(db)
    c = as_user(HR)
    qid = db["drafts"][did]["pool"][0]["id"]
    before = dict(db["drafts"][did]["pool"][0])
    r = c.patch(f"{BASE}/drafts/{did}/questions/{qid}", json=edit)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert db["drafts"][did]["pool"][0] == before and db["audits"] == []
    ok = {"text": "Fine?", "options": ["a", "b", "c", "d"], "answer": 1, "why": "w"}
    assert c.patch(f"{BASE}/drafts/{did}/questions/{qid}", json=ok).status_code == 200
    actor, action, entity_id, diff = db["audits"][0]
    assert (actor, action, entity_id) == (HR.id, "paper_draft.question_edit", did)
    assert diff["question_id"] == qid and diff["from"]["text"] == before["text"]
    assert diff["to"] == {"text": "Fine?", "options": ["a", "b", "c", "d"], "answer": 1, "why": "w"}


def test_edit_unknown_question_404(db):
    did = seed_draft(db)
    ok = {"text": "Fine?", "options": ["a", "b", "c", "d"], "answer": 1, "why": "w"}
    r = as_user(HR).patch(f"{BASE}/drafts/{did}/questions/nope", json=ok)
    assert r.status_code == 404 and r.json()["error"]["code"] == "QUESTION_NOT_FOUND"


@pytest.mark.parametrize("status", ["accepted", "generating", "failed"])
def test_edit_after_accept_conflicts(db, monkeypatch, status):
    saved = []

    async def always_saves(_, did, pool, checks):  # the 409 must come from the service check
        saved.append(did)
        return True

    monkeypatch.setattr(repo, "save_pool_and_checks", always_saves)
    did = seed_draft(db, status=status)
    qid = db["drafts"][did]["pool"][0]["id"]
    ok = {"text": "Fine?", "options": ["a", "b", "c", "d"], "answer": 1, "why": "w"}
    r = as_user(HR).patch(f"{BASE}/drafts/{did}/questions/{qid}", json=ok)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_EDITABLE"
    assert saved == [] and db["audits"] == [] and db["session"].commits == 0


# --- accept ---


def test_failed_checks_block_accept(db):
    settings = PaperSettings.model_validate(SETTINGS)
    did = seed_draft(db, pool=make_pool(settings)[:5])  # pool cannot cover the paper
    r = as_user(HR).post(f"{BASE}/drafts/{did}/accept")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "CHECKS_FAILED" and "pool_covers" in err["details"]["failed"]
    assert db["drafts"][did]["status"] == "draft" and db["audits"] == []


def test_accept_locks_and_audits(db):
    did = seed_draft(db)
    r = as_user(HR).post(f"{BASE}/drafts/{did}/accept")
    assert r.status_code == 200
    assert r.json()["status"] == "accepted" and r.json()["accepted_at"]
    assert db["audits"][0][:3] == (HR.id, "paper_draft.accept", did)
    assert db["session"].commits == 1


def test_accept_twice_conflicts(db):
    did = seed_draft(db)
    c = as_user(HR)
    assert c.post(f"{BASE}/drafts/{did}/accept").status_code == 200
    r = c.post(f"{BASE}/drafts/{did}/accept")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ALREADY_ACCEPTED"


def test_accept_while_generating_conflicts(db):
    did = seed_draft(db, status="generating", pool=[])
    r = as_user(HR).post(f"{BASE}/drafts/{did}/accept")
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_READY"


# --- send ---


def test_send_requires_accepted(db):
    did = seed_draft(db)
    r = as_user(HR).post(f"{BASE}/drafts/{did}/send", json={"candidate_refs": ["d1-c0"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_ACCEPTED"
    assert db["questions"] == {} and db["issues"] == {}


def test_send_persists_questions_once_and_is_idempotent(db, monkeypatch):
    did = seed_draft(db, status="accepted")
    calls = []
    real = repo.persist_questions

    async def counting(session, draft_id, pool):
        calls.append(draft_id)
        return await real(session, draft_id, pool)

    monkeypatch.setattr(repo, "persist_questions", counting)
    c = as_user(HR)
    r1 = c.post(f"{BASE}/drafts/{did}/send", json={"candidate_refs": ["d1-c0", "d1-c1"]})
    assert r1.status_code == 200 and r1.json() == {"sent": 2, "already_sent": 0}
    assert len(db["questions"]) == 20 and calls == [did]
    r2 = c.post(f"{BASE}/drafts/{did}/send", json={"candidate_refs": ["d1-c1", "d1-c2"]})
    assert r2.json() == {"sent": 1, "already_sent": 1}
    assert calls == [did] and len(db["questions"]) == 20  # never written twice
    issued = db["issues"][did]
    assert set(issued) == {"d1-c0", "d1-c1", "d1-c2"}
    assert issued["d1-c0"]["seed"] == "d1|it|d1-c0"
    assert actions(db) == ["paper_draft.send", "paper_draft.send"]
    detail = c.get(f"{BASE}/drafts/{did}").json()
    assert {i["candidate_ref"] for i in detail["issues"]} == {"d1-c0", "d1-c1", "d1-c2"}
    assert detail["persisted_at"]


def test_send_dedupes_refs_and_rejects_empty(db):
    did = seed_draft(db, status="accepted")
    c = as_user(HR)
    r = c.post(f"{BASE}/drafts/{did}/send", json={"candidate_refs": ["a", " a ", "b", "a"]})
    assert r.json() == {"sent": 2, "already_sent": 0}
    assert db["audits"][0][3]["candidate_refs"] == ["a", "b"]
    for refs in ([], ["  ", ""]):
        r = c.post(f"{BASE}/drafts/{did}/send", json={"candidate_refs": refs})
        assert r.status_code == 422 and r.json()["error"]["code"] == "NO_CANDIDATES"
    assert c.post(f"{BASE}/drafts/{did}/send", json={}).status_code == 422


# --- access ---


def test_other_hr_gets_404(db):
    did = seed_draft(db, owner=HR)
    c = as_user(HR2)
    qid = db["drafts"][did]["pool"][0]["id"]
    ok = {"text": "Fine?", "options": ["a", "b", "c", "d"], "answer": 1, "why": "w"}
    responses = [
        c.get(f"{BASE}/drafts/{did}"),
        c.patch(f"{BASE}/drafts/{did}/questions/{qid}", json=ok),
        c.post(f"{BASE}/drafts/{did}/accept"),
        c.post(f"{BASE}/drafts/{did}/send", json={"candidate_refs": ["x"]}),
        c.get(f"{BASE}/drafts/{uuid.uuid4()}"),
    ]
    for r in responses:
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert c.get(f"{BASE}/drafts").json() == []
    assert db["audits"] == [] and db["drafts"][did]["status"] == "draft"


def test_super_admin_sees_all(db):
    a = seed_draft(db, owner=HR)
    b = seed_draft(db, owner=HR2)
    c = as_user(ADMIN)
    assert {d["id"] for d in c.get(f"{BASE}/drafts").json()} == {str(a), str(b)}
    assert c.get(f"{BASE}/drafts/{b}").status_code == 200
    assert c.post(f"{BASE}/drafts/{b}/accept").status_code == 200
    assert db["audits"][0][:2] == (ADMIN.id, "paper_draft.accept")
    assert {d["id"] for d in as_user(HR).get(f"{BASE}/drafts").json()} == {str(a)}


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", f"{BASE}/ai-status"),
        ("post", f"{BASE}/drafts"),
        ("get", f"{BASE}/drafts"),
        ("get", f"{BASE}/drafts/{uuid.uuid4()}"),
        ("patch", f"{BASE}/drafts/{uuid.uuid4()}/questions/q1"),
        ("post", f"{BASE}/drafts/{uuid.uuid4()}/accept"),
        ("post", f"{BASE}/drafts/{uuid.uuid4()}/send"),
    ],
)
def test_unauthenticated_401_non_staff_403(db, monkeypatch, method, path):
    app.dependency_overrides.pop(security.current_user, None)
    client = TestClient(app)
    r = client.request(method.upper(), path, json={})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"

    async def no_user(_, __):
        return None

    async def verified(_):
        return uuid.uuid4()

    monkeypatch.setattr(security.repo, "get_user", no_user)
    monkeypatch.setattr(security, "verify_token", verified)
    r = client.request(method.upper(), path, json={}, headers={"Authorization": "Bearer t"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_STAFF"


@pytest.mark.parametrize(
    "settings",
    [
        SETTINGS | {"mix": {"aptitude": 50, "reasoning": 30, "technical": 30}},
        SETTINGS | {"counts": {"easy": 0, "medium": 0, "advanced": 0}},
        SETTINGS | {"counts": {"easy": -1, "medium": 3, "advanced": 3}},
        SETTINGS | {"track": "sales"},
    ],
)
def test_validation_error_shape(db, settings):
    r = create(as_user(HR), settings=settings)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR" and err["message"] == "Request is invalid"
    assert isinstance(err["details"]["errors"], list) and err["details"]["errors"]
    assert db["drafts"] == {} and db["audits"] == []


# --- concurrency and late jobs ---


def test_mutations_read_draft_with_row_lock(db):
    """edit, accept and send read -> check -> write under `select ... for update`."""
    did = seed_draft(db)
    c = as_user(HR)
    qid = db["drafts"][did]["pool"][0]["id"]
    ok = {"text": "Fine?", "options": ["a", "b", "c", "d"], "answer": 1, "why": "w"}

    def last_read(response):
        assert response.status_code == 200, response.text
        return db["reads"][-1]

    assert last_read(c.patch(f"{BASE}/drafts/{did}/questions/{qid}", json=ok)) == (did, True)
    assert last_read(c.post(f"{BASE}/drafts/{did}/accept")) == (did, True)
    send = c.post(f"{BASE}/drafts/{did}/send", json={"candidate_refs": ["x"]})
    assert last_read(send) == (did, True)
    # plain reads never lock
    assert last_read(c.get(f"{BASE}/drafts/{did}")) == (did, False)


def test_late_generation_does_not_resurrect_timed_out_draft(db):
    did = seed_draft(
        db, status="generating", pool=[], created_at=datetime.now(UTC) - timedelta(minutes=16)
    )
    asyncio.run(service.run_generation(did, HR.id))
    d = db["drafts"][did]
    assert d["status"] == "generating" and d["pool"] == []  # still reported failed on read
    assert "paper_draft.generated" not in actions(db) and db["bg_session"].rollbacks == 1
    r = as_user(HR).get(f"{BASE}/drafts/{did}").json()
    assert r["status"] == "failed" and r["error"] == "Generation timed out"


def test_generation_not_audited_when_finish_is_a_no_op(db, monkeypatch):
    did = seed_draft(db, status="generating", pool=[])

    async def no_op(*_):
        return False

    monkeypatch.setattr(repo, "finish_generation", no_op)
    asyncio.run(service.run_generation(did, HR.id))
    assert db["audits"] == []


# --- HR selection (which pool questions may reach students) ---


def select(c, did, ids):
    return c.put(f"{BASE}/drafts/{did}/selection", json={"selected_ids": ids})


def test_selection_of_exactly_the_paper_gives_everyone_those_questions(db):
    did = seed_draft(db)
    c = as_user(HR)
    picked = [q["id"] for q in c.get(f"{BASE}/drafts/{did}").json()["sample"]]
    r = select(c, did, picked + picked[:1])  # duplicates ignored
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["selected_count"] == 10 and body["pool_size"] == 20
    assert {q["id"] for q in body["pool"] if q["selected"]} == set(picked)
    assert {q["id"] for q in body["sample"]} == set(picked)
    assert body["overlap"]["shared"] == 10  # same questions for everyone, by HR's choice
    checks = {ch["id"]: ch for ch in body["checks"]}
    assert all(ch["ok"] for ch in checks.values()), checks
    assert "students_differ" not in checks
    stored = {q["id"]: q["selected"] for q in db["drafts"][did]["pool"]}
    assert sum(stored.values()) == 10
    assert actions(db) == ["paper_draft.selection"]
    assert db["audits"][0][3] == {"selected": 10, "of": 20}
    assert db["session"].commits == 1
    assert c.post(f"{BASE}/drafts/{did}/accept").status_code == 200


def test_selection_smaller_than_paper_fails_check_and_blocks_accept(db):
    did = seed_draft(db)
    c = as_user(HR)
    ids = [q["id"] for q in db["drafts"][did]["pool"]][:5]
    body = select(c, did, ids).json()
    cover = next(ch for ch in body["checks"] if ch["id"] == "pool_covers")
    assert cover["ok"] is False and "Short of" in cover["detail"]
    r = c.post(f"{BASE}/drafts/{did}/accept")
    assert r.status_code == 409 and r.json()["error"]["code"] == "CHECKS_FAILED"


def test_selection_rejects_unknown_ids_and_locked_drafts(db):
    did = seed_draft(db)
    c = as_user(HR)
    r = select(c, did, ["nope"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "UNKNOWN_QUESTIONS"
    assert db["audits"] == [] and all(q.get("selected", True) for q in db["drafts"][did]["pool"])
    locked = seed_draft(db, status="accepted")
    r = select(c, locked, [db["drafts"][locked]["pool"][0]["id"]])
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_EDITABLE"
    assert db["reads"][-1] == (locked, True)  # read under row lock
    assert (
        as_user(HR2).put(f"{BASE}/drafts/{did}/selection", json={"selected_ids": []}).status_code
        == 404
    )


def test_send_persists_only_selected_questions(db):
    did = seed_draft(db)
    c = as_user(HR)
    picked = [q["id"] for q in c.get(f"{BASE}/drafts/{did}").json()["sample"]]
    select(c, did, picked)
    assert c.post(f"{BASE}/drafts/{did}/accept").status_code == 200
    c.post(f"{BASE}/drafts/{did}/send", json={"candidate_refs": ["d1-c0"]})
    assert set(db["questions"]) == set(picked)


def test_detail_recomputes_checks_so_old_stored_rules_do_not_block(db):
    did = seed_draft(db)
    stale = {
        "id": "students_differ",
        "label": "Students get different papers",
        "ok": False,
        "detail": "x",
    }
    db["drafts"][did]["checks"].append(stale)
    checks = as_user(HR).get(f"{BASE}/drafts/{did}").json()["checks"]
    assert [c["id"] for c in checks if not c["ok"]] == []
    assert "students_differ" not in {c["id"] for c in checks}

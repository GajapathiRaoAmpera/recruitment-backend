"""PUT /rounds/{id}/test and PUT /tests/{id}/questions (FR-13, FR-16). DB is faked; no Judge0."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.execution.test_config import repo, service
from app.main import app

HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
OTHER_HR = StaffUser(id=uuid.uuid4(), email="hr2@x.com", full_name="H R2", role="hr")
ADMIN = StaffUser(id=uuid.uuid4(), email="sa@x.com", full_name="S A", role="super_admin")


class FakeSession:
    commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def db(monkeypatch):
    """One drive owned by HR with a coding round (30 min) and a final round; one test row is
    created on demand. Questions are added by `question()`."""
    state = dict(
        rounds={},  # id -> {type, duration_min, owner}
        tests={},  # id -> row
        questions={},  # id -> {difficulty, marks, approved, complete, kind, owner}
        links=[],  # (test_id, question_id, position)
        attempts=set(),  # test ids someone started
        audits=[],
        order=[],  # call order, to prove audit lands before commit
    )
    session = FakeSession()

    def add_round(kind="coding", owner=HR.id, minutes=30):
        rid = uuid.uuid4()
        state["rounds"][rid] = dict(type=kind, duration_min=minutes, owner=owner)
        return rid

    def question(owner=HR.id, difficulty="easy", approved=True, complete=True, kind="coding"):
        qid = uuid.uuid4()
        state["questions"][qid] = dict(
            difficulty=difficulty,
            marks=5,
            approved=approved,
            complete=complete,
            kind=kind,
            owner=owner,
        )
        return qid

    def seen(owner_id, owner):
        return owner_id is None or owner == owner_id

    async def get_round(_, rid, owner_id):
        r = state["rounds"].get(rid)
        return dict(id=rid, **r) if r and seen(owner_id, r["owner"]) else None

    async def ensure_test(_, rid, seconds):
        if not any(t["round_id"] == rid for t in state["tests"].values()):
            tid = uuid.uuid4()
            state["tests"][tid] = dict(
                id=tid,
                round_id=rid,
                difficulty=None,
                duration_sec=seconds,
                question_count=0,
                settings={},
                opens_at=None,
                closes_at=None,
            )

    async def update_test(_, rid, changes):
        for t in state["tests"].values():
            if t["round_id"] == rid:
                t.update(changes)

    async def get_test_of_round(_, rid):
        return next(t for t in state["tests"].values() if t["round_id"] == rid)

    async def find_test_of_round(_, rid):
        return next((t for t in state["tests"].values() if t["round_id"] == rid), None)

    async def lock_test(_, tid, owner_id):
        t = state["tests"].get(tid)
        return t if t and seen(owner_id, state["rounds"][t["round_id"]]["owner"]) else None

    async def has_attempts(_, tid):
        return tid in state["attempts"]

    async def current_questions(_, tid):
        return [
            dict(
                question_id=q,
                position=p,
                difficulty=state["questions"][q]["difficulty"],
                marks=state["questions"][q]["marks"],
            )
            for (t, q, p) in sorted(state["links"], key=lambda x: x[2])
            if t == tid
        ]

    async def usable_questions(_, ids, owner_id):
        return [
            dict(id=q, **state["questions"][q])
            for q in ids
            if q in state["questions"]
            and state["questions"][q]["kind"] == "coding"
            and seen(owner_id, state["questions"][q]["owner"])
        ]

    async def replace_questions(_, tid, ids):
        state["links"] = [x for x in state["links"] if x[0] != tid]
        state["links"] += [(tid, q, i) for i, q in enumerate(ids)]

    async def set_counts(_, tid, n, levels):
        state["tests"][tid]["question_count"] = n
        state["tests"][tid]["settings"] = {**state["tests"][tid]["settings"], "levels": levels}

    async def audit(_, actor_id, action, entity, entity_id, diff=None):
        state["audits"].append((actor_id, action, entity, entity_id, diff))
        state["order"].append("audit")

    async def commit():
        state["order"].append("commit")
        session.commits += 1

    session.commit = commit
    for name, fn in dict(
        get_round=get_round,
        ensure_test=ensure_test,
        update_test=update_test,
        get_test_of_round=get_test_of_round,
        find_test_of_round=find_test_of_round,
        lock_test=lock_test,
        has_attempts=has_attempts,
        current_questions=current_questions,
        usable_questions=usable_questions,
        replace_questions=replace_questions,
        set_counts=set_counts,
    ).items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(service, "audit", audit)
    app.dependency_overrides[get_session] = lambda: session
    state.update(session=session, add_round=add_round, question=question)
    yield state
    app.dependency_overrides.clear()


def as_user(user):
    app.dependency_overrides[security.current_user] = lambda: user
    return TestClient(app)


def make_test(db, owner=HR):
    rid = db["add_round"]()
    r = as_user(owner).put(f"/api/v1/rounds/{rid}/test", json={})
    assert r.status_code == 200
    db["audits"].clear()
    db["order"].clear()
    return uuid.UUID(r.json()["id"])


def put_questions(user, test_id, ids):
    return as_user(user).put(
        f"/api/v1/tests/{test_id}/questions", json={"question_ids": [str(i) for i in ids]}
    )


# --- roles -----------------------------------------------------------------------------------


def test_both_routes_need_a_staff_login(db):
    c = TestClient(app)
    for path in (f"/api/v1/rounds/{uuid.uuid4()}/test", f"/api/v1/tests/{uuid.uuid4()}/questions"):
        assert c.put(path, json={}).status_code == 401, path


def test_an_unknown_account_gets_403(db):
    """A signed-in user with no staff row is refused (the only roles are hr and super_admin)."""
    from app.core.errors import AppError

    async def not_staff():
        raise AppError(403, "NOT_STAFF", "This account has no access to Campus Hire")

    tid = make_test(db)
    app.dependency_overrides[security.current_user] = not_staff
    c = TestClient(app)
    assert c.put(f"/api/v1/tests/{tid}/questions", json={"question_ids": []}).status_code == 403


# --- PUT /rounds/{id}/test -------------------------------------------------------------------


def test_creates_the_test_with_the_rounds_duration_and_audits(db):
    rid = db["add_round"](minutes=45)
    r = as_user(HR).put(f"/api/v1/rounds/{rid}/test", json={})
    assert r.status_code == 200
    out = r.json()
    assert (
        out["round_id"] == str(rid) and out["duration_sec"] == 2700 and out["question_count"] == 0
    )
    (actor, action, entity, entity_id, diff) = db["audits"][0]
    assert (actor, action, entity, str(entity_id)) == (HR.id, "test.configure", "tests", out["id"])
    assert diff == {"round_id": str(rid), "fields": []}
    assert db["order"] == ["audit", "commit"]


def test_a_round_without_a_duration_gets_the_default(db):
    rid = db["add_round"](minutes=None)
    assert as_user(HR).put(f"/api/v1/rounds/{rid}/test", json={}).json()["duration_sec"] == 1800


def test_only_the_fields_sent_change_and_repeat_calls_keep_one_test(db):
    rid = db["add_round"]()
    first = as_user(HR).put(f"/api/v1/rounds/{rid}/test", json={"difficulty": "medium"}).json()
    second = as_user(HR).put(f"/api/v1/rounds/{rid}/test", json={"duration_sec": 3600}).json()
    assert first["id"] == second["id"] and len(db["tests"]) == 1
    assert second["difficulty"] == "medium" and second["duration_sec"] == 3600
    assert db["audits"][-1][4]["fields"] == ["duration_sec"]


@pytest.mark.parametrize(
    "body",
    [
        {"duration_sec": 59},
        {"duration_sec": 36_001},
        {"difficulty": "hard"},
        {"question_count": 3},  # set through PUT /tests/{id}/questions
        {"opens_at": "2026-11-12T10:00:00Z", "closes_at": "2026-11-12T09:00:00Z"},
    ],
)
def test_configure_rejects_bad_bodies(db, body):
    rid = db["add_round"]()
    r = as_user(HR).put(f"/api/v1/rounds/{rid}/test", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert db["tests"] == {} and db["session"].commits == 0


def test_configure_hides_other_hrs_round_and_unknown_round(db):
    rid = db["add_round"](owner=OTHER_HR.id)
    for round_id in (rid, uuid.uuid4()):
        r = as_user(HR).put(f"/api/v1/rounds/{round_id}/test", json={})
        assert r.status_code == 404 and r.json()["error"]["code"] == "ROUND_NOT_FOUND"
    assert db["tests"] == {}
    assert as_user(ADMIN).put(f"/api/v1/rounds/{rid}/test", json={}).status_code == 200


@pytest.mark.parametrize("kind", ["shortlist", "ai_interview", "final"])
def test_rounds_without_a_test_are_refused(db, kind):
    rid = db["add_round"](kind=kind)
    r = as_user(HR).put(f"/api/v1/rounds/{rid}/test", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROUND_HAS_NO_TEST"
    assert db["tests"] == {}


# --- PUT /tests/{id}/questions: happy path ---------------------------------------------------


def test_sets_questions_in_order_with_levels_counts_and_audit(db):
    tid = make_test(db)
    a, b, c = db["question"](), db["question"](difficulty="advanced"), db["question"]()
    r = put_questions(HR, tid, [b, a, c])
    assert r.status_code == 200
    out = r.json()
    assert out["test_id"] == str(tid) and out["question_count"] == 3
    assert out["levels"] == {"easy": 2, "medium": 0, "advanced": 1}
    assert [(q["question_id"], q["position"]) for q in out["questions"]] == [
        (str(b), 0),
        (str(a), 1),
        (str(c), 2),
    ]
    assert db["tests"][tid]["question_count"] == 3
    assert db["tests"][tid]["settings"]["levels"] == {"easy": 2, "medium": 0, "advanced": 1}
    (actor, action, entity, entity_id, diff) = db["audits"][0]
    assert (actor, action, entity, entity_id) == (HR.id, "test.set_questions", "tests", tid)
    assert diff["after"] == [str(b), str(a), str(c)] and diff["before"] == []
    assert db["order"] == ["audit", "commit"]  # same transaction: audit before the one commit


def test_replaces_the_previous_list_and_can_clear_it(db):
    tid = make_test(db)
    a, b = db["question"](), db["question"](difficulty="medium")
    put_questions(HR, tid, [a])
    r = put_questions(HR, tid, [b])
    assert [q["question_id"] for q in r.json()["questions"]] == [str(b)]
    assert {x[1] for x in db["links"]} == {b}
    assert db["audits"][-1][4]["before"] == [str(a)]
    cleared = put_questions(HR, tid, [])
    assert cleared.status_code == 200 and cleared.json()["question_count"] == 0
    assert db["links"] == []


def test_the_same_list_again_is_a_no_op_even_after_students_started(db):
    tid = make_test(db)
    a = db["question"]()
    put_questions(HR, tid, [a])
    db["audits"].clear()
    db["attempts"].add(tid)
    commits = db["session"].commits
    r = put_questions(HR, tid, [a])
    assert r.status_code == 200 and r.json()["question_count"] == 1
    assert db["audits"] == [] and db["session"].commits == commits


def test_super_admin_can_use_any_hr_s_test_and_questions(db):
    tid = make_test(db)
    q = db["question"](owner=OTHER_HR.id)
    assert put_questions(ADMIN, tid, [q]).status_code == 200


# --- PUT /tests/{id}/questions: refusals -----------------------------------------------------


def test_other_hr_and_unknown_test_get_404(db):
    tid = make_test(db)
    for test_id, user in ((tid, OTHER_HR), (uuid.uuid4(), HR)):
        r = put_questions(user, test_id, [])
        assert r.status_code == 404 and r.json()["error"]["code"] == "TEST_NOT_FOUND"
    assert db["audits"] == []


def test_a_started_test_keeps_its_questions(db):
    tid = make_test(db)
    a, b = db["question"](), db["question"]()
    put_questions(HR, tid, [a])
    db["attempts"].add(tid)
    db["audits"].clear()
    r = put_questions(HR, tid, [a, b])
    assert r.status_code == 409 and r.json()["error"]["code"] == "TEST_IN_USE"
    assert [x[1] for x in db["links"]] == [a] and db["audits"] == []


def test_unknown_foreign_and_non_coding_questions_are_listed_as_missing(db):
    tid = make_test(db)
    mine = db["question"]()
    foreign = db["question"](owner=OTHER_HR.id)
    mcq = db["question"](kind="mcq")
    ghost = uuid.uuid4()
    commits = db["session"].commits
    r = put_questions(HR, tid, [mine, foreign, mcq, ghost])
    assert r.status_code == 422 and r.json()["error"]["code"] == "QUESTIONS_NOT_FOUND"
    assert sorted(r.json()["error"]["details"]["question_ids"]) == sorted(
        [str(foreign), str(mcq), str(ghost)]
    )
    assert db["links"] == [] and db["audits"] == [] and db["session"].commits == commits


def test_unapproved_questions_are_refused(db):
    tid = make_test(db)
    draft = db["question"](approved=False)
    r = put_questions(HR, tid, [db["question"](), draft])
    assert r.status_code == 422 and r.json()["error"]["code"] == "QUESTIONS_NOT_APPROVED"
    assert r.json()["error"]["details"]["question_ids"] == [str(draft)]
    assert db["links"] == []


def test_questions_without_exactly_3_plus_15_cases_are_refused(db):
    tid = make_test(db)
    thin = db["question"](complete=False)
    r = put_questions(HR, tid, [thin])
    assert r.status_code == 422 and r.json()["error"]["code"] == "QUESTIONS_INCOMPLETE"
    assert r.json()["error"]["details"]["question_ids"] == [str(thin)]


def test_a_failed_replace_leaves_the_old_list(db):
    tid = make_test(db)
    a = db["question"]()
    put_questions(HR, tid, [a])
    put_questions(HR, tid, [a, uuid.uuid4()])
    assert [x[1] for x in db["links"]] == [a]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question_ids": "x"},
        {"question_ids": ["not-a-uuid"]},
        {"question_ids": [str(uuid.UUID(int=1))] * 2},  # repeated
        {"question_ids": [str(uuid.uuid4()) for _ in range(51)]},
        {"question_ids": [], "levels": {}},
    ],
)
def test_bad_bodies_are_422(db, body):
    tid = make_test(db)
    r = as_user(HR).put(f"/api/v1/tests/{tid}/questions", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_response_has_no_test_cases_or_question_text(db):
    tid = make_test(db)
    q = db["question"]()
    out = put_questions(HR, tid, [q]).json()
    assert set(out["questions"][0]) == {"question_id", "position", "difficulty", "marks"}


# --- GET /rounds/{id}/test -------------------------------------------------------------------


def test_read_needs_a_staff_login(db):
    assert TestClient(app).get(f"/api/v1/rounds/{uuid.uuid4()}/test").status_code == 401


def test_read_returns_the_config_with_questions_in_order(db):
    tid = make_test(db)
    rid = db["tests"][tid]["round_id"]
    as_user(HR).put(f"/api/v1/rounds/{rid}/test", json={"difficulty": "medium"})
    q1, q2, q3 = db["question"](), db["question"](difficulty="advanced"), db["question"]()
    assert put_questions(HR, tid, [q2, q3, q1]).status_code == 200
    commits, audits = db["session"].commits, len(db["audits"])
    r = as_user(HR).get(f"/api/v1/rounds/{rid}/test")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["id"] == str(tid) and out["round_id"] == str(rid)
    assert out["question_ids"] == [str(q2), str(q3), str(q1)]
    assert out["difficulty"] == "medium" and out["duration_sec"] == 1800
    assert out["question_count"] == 3 and out["locked"] is False
    assert out["settings"]["levels"] == {"easy": 2, "medium": 0, "advanced": 1}
    assert set(out) == {
        "id", "round_id", "difficulty", "duration_sec", "question_count", "settings",
        "opens_at", "closes_at", "question_ids", "locked",
    }  # fmt: skip
    assert db["session"].commits == commits  # a read: no commit
    assert len(db["audits"]) == audits  # a read: no audit row


def test_read_of_a_round_with_no_test_is_404_and_creates_nothing(db):
    rid = db["add_round"]()
    r = as_user(HR).get(f"/api/v1/rounds/{rid}/test")
    assert r.status_code == 404 and r.json()["error"]["code"] == "TEST_NOT_FOUND"
    assert db["tests"] == {} and db["audits"] == [] and db["session"].commits == 0


def test_read_hides_other_hrs_round_and_unknown_round(db):
    tid = make_test(db)
    rid = db["tests"][tid]["round_id"]
    for user, round_id in ((OTHER_HR, rid), (HR, uuid.uuid4())):
        r = as_user(user).get(f"/api/v1/rounds/{round_id}/test")
        assert r.status_code == 404 and r.json()["error"]["code"] == "ROUND_NOT_FOUND"


def test_super_admin_can_read_any_round(db):
    tid = make_test(db)
    rid = db["tests"][tid]["round_id"]
    r = as_user(ADMIN).get(f"/api/v1/rounds/{rid}/test")
    assert r.status_code == 200 and r.json()["id"] == str(tid)


@pytest.mark.parametrize("kind", ["shortlist", "ai_interview", "final"])
def test_read_of_a_round_without_a_test_type_is_409(db, kind):
    rid = db["add_round"](kind=kind)
    r = as_user(HR).get(f"/api/v1/rounds/{rid}/test")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROUND_HAS_NO_TEST"


def test_read_shows_locked_once_a_student_started(db):
    tid = make_test(db)
    rid = db["tests"][tid]["round_id"]
    assert as_user(HR).get(f"/api/v1/rounds/{rid}/test").json()["locked"] is False
    db["attempts"].add(tid)
    assert as_user(HR).get(f"/api/v1/rounds/{rid}/test").json()["locked"] is True

"""POST/GET/PATCH /questions for coding questions (FR-16, AC-5). DB is faked; no Judge0."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.execution.coding_questions import repo, service
from app.main import app

HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
OTHER_HR = StaffUser(id=uuid.uuid4(), email="hr2@x.com", full_name="H R2", role="hr")
ADMIN = StaffUser(id=uuid.uuid4(), email="sa@x.com", full_name="S A", role="super_admin")
NOW = datetime.now(UTC)
HIDDEN_EXPECTED = "TOP-SECRET-EXPECTED"


def cases(samples=3, hidden=15):
    return [{"input": f"s{i}", "expected": f"{i}", "is_sample": True} for i in range(samples)] + [
        {"input": f"h{i}", "expected": HIDDEN_EXPECTED + str(i), "is_sample": False}
        for i in range(hidden)
    ]


def body(**over):
    return {
        "kind": "coding",
        "body": "Print the sum.",
        "difficulty": "easy",
        "tags": ["arrays"],
        "marks": 5,
        "test_cases": cases(),
        **over,
    }


class FakeSession:
    commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def db(monkeypatch):
    questions, case_rows, audits, state = {}, {}, [], {"answers": False}
    session = FakeSession()

    async def insert_question(
        _, owner, body_, difficulty, tags, marks, mode="stdin", signature=None
    ):
        row = dict(
            mode=mode,
            signature=signature,
            id=uuid.uuid4(),
            body=body_,
            difficulty=difficulty,
            tags=tags,
            marks=marks,
            approved=True,
            created_by=owner,
            created_at=NOW,
        )
        questions[row["id"]] = row
        return row

    async def insert_cases(_, qid, cs):
        case_rows[qid] = [
            dict(id=uuid.uuid4(), position=i, **c.model_dump()) for i, c in enumerate(cs)
        ]

    async def delete_cases(_, qid):
        case_rows[qid] = []

    async def get_question(_, qid, owner):
        q = questions.get(qid)
        return q if q and (owner is None or q["created_by"] == owner) else None

    async def get_cases(_, qid):
        return case_rows.get(qid, [])

    async def list_questions(_, owner, difficulty, tags, approved, limit):
        return [
            q
            for q in questions.values()
            if (owner is None or q["created_by"] == owner)
            and (difficulty is None or q["difficulty"] == difficulty)
        ][:limit]

    async def has_answers(_, qid):
        return state["answers"]

    async def update_question(_, qid, changes):
        questions[qid] = {**questions[qid], **changes}
        return questions[qid]

    async def audit(_, actor_id, action, entity, entity_id, diff=None):
        audits.append((actor_id, action, entity_id, diff))

    for name, fn in dict(
        insert_question=insert_question,
        insert_cases=insert_cases,
        delete_cases=delete_cases,
        get_question=get_question,
        get_cases=get_cases,
        list_questions=list_questions,
        has_answers=has_answers,
        update_question=update_question,
    ).items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(service, "audit", audit)
    app.dependency_overrides[get_session] = lambda: session
    yield dict(questions=questions, cases=case_rows, audits=audits, session=session, state=state)
    app.dependency_overrides.clear()


def as_user(user):
    app.dependency_overrides[security.current_user] = lambda: user
    return TestClient(app)


def create(user=HR, **over):
    return as_user(user).post("/api/v1/questions", json=body(**over))


# --- roles -----------------------------------------------------------------------------------


def test_every_route_needs_a_staff_login(db):
    c = TestClient(app)
    qid = uuid.uuid4()
    for method, path in [
        ("post", "/api/v1/questions"),
        ("get", "/api/v1/questions"),
        ("get", f"/api/v1/questions/{qid}"),
        ("patch", f"/api/v1/questions/{qid}"),
    ]:
        r = c.request(method.upper(), path, json={})
        assert r.status_code == 401, path


# --- create ----------------------------------------------------------------------------------


def test_create_stores_question_cases_and_audit_in_one_commit(db):
    r = create()
    assert r.status_code == 201
    out = r.json()
    assert out["kind"] == "coding" and out["approved"] is True and out["created_by"] == str(HR.id)
    assert [c["is_sample"] for c in out["test_cases"]] == [True] * 3 + [False] * 15
    assert [c["position"] for c in out["test_cases"]] == list(range(18))
    assert db["session"].commits == 1
    (actor, action, entity_id, diff) = db["audits"][0]
    assert (actor, action, entity_id) == (HR.id, "question.create", uuid.UUID(out["id"]))
    assert diff == {"kind": "coding", "difficulty": "easy", "test_cases": 18}
    assert HIDDEN_EXPECTED not in str(db["audits"])  # the audit log holds counts, not cases


def test_create_cleans_tags(db):
    out = create(tags=[" arrays ", "arrays", "", "dp"]).json()
    assert out["tags"] == ["arrays", "dp"]


@pytest.mark.parametrize(
    "over",
    [
        {"test_cases": cases(samples=3, hidden=14)},
        {"test_cases": cases(samples=2, hidden=16)},
        {"test_cases": cases(samples=4, hidden=15)},
        {"test_cases": []},
        {"test_cases": [{**c, "expected": "  \n"} for c in cases()]},
        {"test_cases": [{**c, "extra": 1} for c in cases()]},
        {"test_cases": [{**c, "input": "x" * 20_001} for c in cases()]},
        {"kind": "mcq"},
        {"difficulty": "hard"},
        {"body": "   "},
        {"marks": 0},
        {"marks": 1000},
        {"tags": [f"t{i}" for i in range(11)]},
        {"unknown": 1},
    ],
)
def test_create_rejects_bad_bodies(db, over):
    r = create(**over)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert db["questions"] == {} and db["session"].commits == 0


def test_the_three_sample_rule_error_names_the_counts(db):
    r = create(test_cases=cases(samples=3, hidden=14))
    assert "3 sample and 15 hidden" in str(r.json()) and "got 3 sample and 14 hidden" in str(
        r.json()
    )


def test_empty_stdin_is_allowed(db):
    cs = cases()
    cs[0]["input"] = ""
    assert create(test_cases=cs).status_code == 201


# --- read ------------------------------------------------------------------------------------


def test_owner_reads_cases_other_hr_gets_404_admin_reads(db):
    qid = create().json()["id"]
    got = as_user(HR).get(f"/api/v1/questions/{qid}")
    assert got.status_code == 200 and len(got.json()["test_cases"]) == 18
    other = as_user(OTHER_HR).get(f"/api/v1/questions/{qid}")
    assert other.status_code == 404 and other.json()["error"]["code"] == "QUESTION_NOT_FOUND"
    assert as_user(ADMIN).get(f"/api/v1/questions/{qid}").status_code == 200


def test_list_is_scoped_to_owner_and_has_no_cases(db):
    create(HR)
    create(OTHER_HR, difficulty="advanced")
    mine = as_user(HR).get("/api/v1/questions").json()
    assert len(mine) == 1 and "test_cases" not in mine[0]
    assert len(as_user(ADMIN).get("/api/v1/questions").json()) == 2
    only_adv = as_user(ADMIN).get("/api/v1/questions?difficulty=advanced").json()
    assert len(only_adv) == 1


def test_list_rejects_other_kinds_and_bad_difficulty(db):
    assert as_user(HR).get("/api/v1/questions?kind=mcq").json()["error"]["code"] == (
        "UNSUPPORTED_KIND"
    )
    assert as_user(HR).get("/api/v1/questions?difficulty=hard").status_code == 422


# --- edit ------------------------------------------------------------------------------------


def test_patch_changes_only_what_is_sent_and_audits(db):
    qid = create().json()["id"]
    db["audits"].clear()
    r = as_user(HR).patch(f"/api/v1/questions/{qid}", json={"difficulty": "medium", "marks": 2})
    assert r.status_code == 200
    assert r.json()["difficulty"] == "medium" and len(r.json()["test_cases"]) == 18
    assert db["audits"][0][1:] == (
        "question.update",
        uuid.UUID(qid),
        {"fields": ["difficulty", "marks"], "test_cases_replaced": False},
    )


def test_patch_replaces_all_cases(db):
    qid = create().json()["id"]
    new = [{**c, "expected": "new" + c["input"]} for c in cases()]
    r = as_user(HR).patch(f"/api/v1/questions/{qid}", json={"test_cases": new})
    assert r.status_code == 200
    assert {c["expected"] for c in r.json()["test_cases"] if not c["is_sample"]} == {
        f"newh{i}" for i in range(15)
    }


def test_patch_cases_still_need_three_and_fifteen(db):
    qid = create().json()["id"]
    r = as_user(HR).patch(f"/api/v1/questions/{qid}", json={"test_cases": cases(3, 10)})
    assert r.status_code == 422


def test_patch_cases_blocked_once_students_answered(db):
    qid = create().json()["id"]
    db["state"]["answers"] = True
    r = as_user(HR).patch(f"/api/v1/questions/{qid}", json={"test_cases": cases()})
    assert r.status_code == 409 and r.json()["error"]["code"] == "QUESTION_IN_USE"
    # text-only edits are still fine
    assert (
        as_user(HR).patch(f"/api/v1/questions/{qid}", json={"body": "New text"}).status_code == 200
    )


@pytest.mark.parametrize(
    "patch", [{}, {"body": None}, {"difficulty": "hard"}, {"approved": None}, {"kind": "mcq"}]
)
def test_patch_rejects_bad_bodies(db, patch):
    qid = create().json()["id"]
    assert as_user(HR).patch(f"/api/v1/questions/{qid}", json=patch).status_code == 422


def test_patch_by_another_hr_is_404_and_changes_nothing(db):
    qid = create().json()["id"]
    r = as_user(OTHER_HR).patch(f"/api/v1/questions/{qid}", json={"body": "hijack"})
    assert r.status_code == 404
    assert db["questions"][uuid.UUID(qid)]["body"] == "Print the sum."

"""GET /tests/{id}/results and /tests/{id}/results/{attempt_id} (HR dashboard). DB is faked."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.execution.results import repo
from app.main import app

HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
OTHER_HR = StaffUser(id=uuid.uuid4(), email="hr2@x.com", full_name="H R2", role="hr")
ADMIN = StaffUser(id=uuid.uuid4(), email="sa@x.com", full_name="S A", role="super_admin")
NOW = datetime.now(UTC)
TEST = uuid.uuid4()
Q1, Q2, Q3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
A1, A2 = uuid.uuid4(), uuid.uuid4()


def attempt(aid, name, status="submitted"):
    return dict(
        attempt_id=aid,
        candidate_id=uuid.uuid4(),
        student_id=f"S-{name}",
        full_name=name,
        status=status,
        started_at=NOW,
        submitted_at=NOW,
    )


def answer(aid, qid, passed, total=18, marks=None, code="print(1)"):
    return dict(
        attempt_id=aid,
        question_id=qid,
        language="python",
        passed_cases=passed,
        total_cases=total,
        marks=marks,
        code=code,
        updated_at=NOW,
    )


@pytest.fixture
def db(monkeypatch):
    state = dict(
        owner=HR.id,
        questions=[
            dict(question_id=Q1, position=0, difficulty="easy", marks=Decimal(10)),
            dict(question_id=Q2, position=1, difficulty="advanced", marks=Decimal(30)),
            dict(question_id=Q3, position=2, difficulty="medium", marks=Decimal(5)),
        ],
        attempts=[attempt(A1, "Asha"), attempt(A2, "Ben", "in_progress")],
        answers=[
            answer(A1, Q1, 18),
            answer(A1, Q2, 9),  # half the cases: half of 30
            answer(A2, Q1, 6),  # 6/18 of 10 = 3.33
        ],
        asked=[],
    )

    async def get_test(_, test_id, owner_id):
        return test_id == TEST and (owner_id is None or owner_id == state["owner"])

    async def coding_questions(_, test_id):
        return state["questions"]

    async def attempts(_, test_id, limit):
        state["asked"].append(("attempts", limit))
        return state["attempts"][:limit]

    async def attempt_(_, test_id, attempt_id):
        return next((a for a in state["attempts"] if a["attempt_id"] == attempt_id), None)

    async def answers(_, test_id, ids, *, with_code):
        state["asked"].append(("answers", with_code))
        return [a for a in state["answers"] if a["attempt_id"] in ids]

    for name, fn in dict(
        get_test=get_test,
        coding_questions=coding_questions,
        attempts=attempts,
        attempt=attempt_,
        answers=answers,
    ).items():
        monkeypatch.setattr(repo, name, fn)
    app.dependency_overrides[get_session] = lambda: object()
    yield state
    app.dependency_overrides.clear()


def as_user(user):
    app.dependency_overrides[security.current_user] = lambda: user
    return TestClient(app)


def test_both_routes_need_a_staff_login(db):
    c = TestClient(app)
    assert c.get(f"/api/v1/tests/{TEST}/results").status_code == 401
    assert c.get(f"/api/v1/tests/{TEST}/results/{A1}").status_code == 401


def test_other_hr_and_unknown_test_get_404(db):
    for user, tid in ((OTHER_HR, TEST), (HR, uuid.uuid4())):
        r = as_user(user).get(f"/api/v1/tests/{tid}/results")
        assert r.status_code == 404 and r.json()["error"]["code"] == "TEST_NOT_FOUND"
        r = as_user(user).get(f"/api/v1/tests/{tid}/results/{A1}")
        assert r.status_code == 404 and r.json()["error"]["code"] == "TEST_NOT_FOUND"
    assert as_user(ADMIN).get(f"/api/v1/tests/{TEST}/results").status_code == 200


def test_results_score_each_question_as_marks_times_pass_fraction(db):
    out = as_user(HR).get(f"/api/v1/tests/{TEST}/results").json()
    assert out["max_score"] == "45.00"
    assert [q["question_id"] for q in out["questions"]] == [str(Q1), str(Q2), str(Q3)]
    asha, ben = out["candidates"]
    assert asha["score"] == "25.00"  # 10 + 15 + 0 (Q3 never submitted)
    assert [a["marks_earned"] for a in asha["answers"]] == ["10.00", "15.00", "0.00"]
    assert [a["submitted"] for a in asha["answers"]] == [True, True, False]
    assert asha["answers"][0]["passed"] == 18 and asha["answers"][2]["passed"] is None
    assert ben["score"] == "3.33" and ben["status"] == "in_progress"


def test_a_stored_mark_wins_over_the_pass_fraction(db):
    db["answers"][0] = answer(A1, Q1, 18, marks=Decimal("7.5"))
    out = as_user(HR).get(f"/api/v1/tests/{TEST}/results").json()
    assert out["candidates"][0]["answers"][0]["marks_earned"] == "7.50"


def test_results_carry_counts_never_code_or_case_text(db):
    r = as_user(HR).get(f"/api/v1/tests/{TEST}/results")
    assert "code" not in r.text and "expected" not in r.text and "input" not in r.text
    assert ("answers", False) in db["asked"]


def test_results_limit_is_applied_and_bounded(db):
    out = as_user(HR).get(f"/api/v1/tests/{TEST}/results?limit=1").json()
    assert len(out["candidates"]) == 1
    for bad in (0, 1001):
        assert as_user(HR).get(f"/api/v1/tests/{TEST}/results?limit={bad}").status_code == 422


def test_a_test_with_no_attempts_is_empty_not_an_error(db):
    db["attempts"].clear()
    out = as_user(HR).get(f"/api/v1/tests/{TEST}/results").json()
    assert out["candidates"] == [] and out["max_score"] == "45.00"


def test_submissions_show_the_code_per_question(db):
    r = as_user(HR).get(f"/api/v1/tests/{TEST}/results/{A1}")
    assert r.status_code == 200
    out = r.json()
    assert (out["student_id"], out["score"], out["max_score"]) == ("S-Asha", "25.00", "45.00")
    first, second, third = out["submissions"]
    assert first["code"] == "print(1)" and first["language"] == "python"
    assert third["submitted"] is False and third["code"] is None
    assert ("answers", True) in db["asked"]


def test_submissions_of_an_attempt_from_another_test_is_404(db):
    r = as_user(HR).get(f"/api/v1/tests/{TEST}/results/{uuid.uuid4()}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "ATTEMPT_NOT_FOUND"

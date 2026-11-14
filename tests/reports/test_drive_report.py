"""GET /drives/{id}/report.pdf: closed drive -> branded PDF. DB faked like tests/advance."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth import security
from app.auth.schemas import StaffUser
from app.core.db import get_session
from app.main import app
from app.reports import repo, service

HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="Priya Raman", role="hr")
OTHER_HR = StaffUser(id=uuid.uuid4(), email="hr2@x.com", full_name="Other", role="hr")
DRIVE = uuid.uuid4()
APT, AI, FINAL, SL = (uuid.uuid4() for _ in range(4))
T0 = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 14, 13, 10, tzinfo=UTC)


def drive(status="closed"):
    return dict(id=DRIVE, company="Ampera Technologies", campus="SIT", role_title="SDE",
                status=status, created_at=T0, owner_name="Priya Raman",
                applied=5, shortlisted=4)  # fmt: skip


def rnd(rid, pos, type_, threshold=None, duration=None, day=1):
    at = T0 + timedelta(days=day)
    return dict(id=rid, position=pos, type=type_, status="completed", threshold=threshold,
                duration_sec=duration, first_at=at,
                last_at=at + timedelta(hours=1))  # fmt: skip


ROUNDS = [rnd(SL, 1, "shortlist"), rnd(APT, 2, "aptitude", 60, 3600, 3),
          rnd(AI, 3, "ai_interview", day=6), rnd(FINAL, 4, "final", day=8)]  # fmt: skip
C = [uuid.uuid4() for _ in range(4)]


def row(round_id, i, name, status, score=None, tabs=0, note=None):
    return dict(round_id=round_id, candidate_id=C[i], student_id=f"S{i}", full_name=name,
                status=status, score=score, rank=None, hr_note=note, attempt_status=None,
                tab_switches=tabs)  # fmt: skip


ROWS = [
    row(APT, 0, "Asha", "passed", 100, tabs=1),
    row(APT, 1, "Ben", "passed", 75.5),
    row(APT, 2, "Cara", "failed", 40, tabs=3),
    row(APT, 3, "Dev", "absent"),
    row(AI, 0, "Asha", "passed", 4.25),
    row(AI, 1, "Ben", "failed", 3.75, note="Could not explain own code"),
    row(FINAL, 0, "Asha", "selected", 4.25),
]
LOG = [
    dict(action="candidate.shortlist", entity_id=str(DRIVE), diff={"shortlisted": True, "count": 4},
         created_at=T0, actor="Priya Raman"),
    dict(action="round.advance", entity_id=str(APT),
         diff='{"threshold": 60, "passed": 2, "failed": 1, "absent": 1}',
         created_at=T0, actor="Priya Raman"),
    dict(action="round.decide", entity_id=str(FINAL), diff={"passed": [str(C[0])], "failed": 0,
         "drive_closed": True}, created_at=T0, actor="Priya Raman"),
]  # fmt: skip


def build(**kw):
    args = dict(drive=drive(), rounds=ROUNDS, rows=ROWS, log=LOG, by=HR, now=NOW) | kw
    return service.build(**args)


def test_build_counts_funnel_and_selection():
    d = build()
    assert [(f["stage"], f["count"]) for f in d["funnel"]] == [
        ("Applied", 5), ("Shortlisted", 4), ("Passed Aptitude test", 2), ("Passed AI interview", 1),
        ("Selected", 1),
    ]  # fmt: skip
    assert d["headline"][2] == {"label": "Selection ratio", "value": "20%"}
    assert d["score_heads"] == ["Aptitude test", "AI interview", "Final round"]
    assert d["selected"] == [
        {"name": "Asha", "sid": "S0", "scores": ["100%", "4.25 / 5", "4.25 / 5"]}
    ]
    assert d["drive"]["start"] == "03 Nov 2026" and d["generated_at"] == "14 Nov 2026, 18:40 IST"


def test_build_round_stats():
    apt, ai = build()["rounds"][:2]
    assert (apt["invited"], apt["sat"], apt["absent"], apt["passed"], apt["failed"]) == (
        4,
        3,
        1,
        2,
        1,
    )
    assert apt["cutoff"] == "60%" and apt["duration_min"] == 60
    assert (
        apt["buckets"][9] == 1 and apt["buckets"][7] == 1 and apt["buckets"][4] == 1
    )  # 100% -> last band
    assert apt["flags"] == {"tab_switches": 2, "locked": 1}
    assert [t["name"] for t in apt["top"]] == ["Asha", "Ben", "Cara"]
    assert ai["cutoff"] == "HR decision" and ai["decisions"][1] == {
        "name": "Ben", "sid": "S1", "ai": "3.75 / 5", "decision": "Fail",
        "note": "Could not explain own code",
    }  # fmt: skip


def test_build_log_reads_as_sentences():
    log = [e["action"] for e in build()["log"]]
    assert log == [
        "Shortlisted 4 candidates",
        "Advanced the Aptitude test at 60% cut-off: 2 passed, 1 failed, 1 absent",
        "Decided the Final round: 1 passed, 0 failed; drive closed",
    ]


def test_render_is_a_pdf_and_candidate_text_is_never_markup():
    evil = '#panic("x") $x^2$ *b* _i_ \\ "q" <l> @r // c'
    rows = ROWS + [row(AI, 2, evil, "failed", 2.0, note='#read("/etc/passwd")')]
    pdf = service.render(build(rows=rows))
    assert pdf[:5] == b"%PDF-"


def test_render_empty_drive():
    d = drive() | {"applied": 0, "shortlisted": 0}
    assert service.render(build(drive=d, rounds=[], rows=[], log=[]))[:5] == b"%PDF-"


# ---------------------------------------------------------------- API


class FakeSession:
    def __init__(self, state):
        self.state = state

    async def commit(self):
        self.state["commits"] += 1


@pytest.fixture
def db(monkeypatch):
    state = dict(drive=drive(), audits=[], commits=0)

    async def get_drive(_, did, owner):
        if did != DRIVE or (owner is not None and owner != HR.id):
            return None
        return state["drive"]

    async def rounds(_, did):
        return ROUNDS

    async def candidate_rows(_, did):
        return ROWS

    async def decision_log(_, did):
        return LOG

    async def audit(_, actor, action, entity, eid, diff=None):
        state["audits"].append((action, eid))

    for name, fn in dict(get_drive=get_drive, rounds=rounds, candidate_rows=candidate_rows,
                         decision_log=decision_log).items():  # fmt: skip
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(service, "audit", audit)
    monkeypatch.setattr(service, "render", lambda data: b"%PDF-fake")
    app.dependency_overrides[get_session] = lambda: FakeSession(state)
    app.dependency_overrides[security.current_user] = lambda: HR
    yield state
    app.dependency_overrides.clear()


URL = f"/api/v1/drives/{DRIVE}/report.pdf"


def test_download_closed_drive(db):
    r = TestClient(app).get(URL)
    assert r.status_code == 200 and r.content == b"%PDF-fake"
    assert r.headers["content-type"] == "application/pdf"
    assert (
        r.headers["content-disposition"]
        == 'attachment; filename="Ampera-Technologies-SDE-report.pdf"'
    )
    assert db["audits"] == [("drive.report", DRIVE)] and db["commits"] == 1


def test_running_drive_is_409(db):
    db["drive"] = drive(status="active")
    r = TestClient(app).get(URL)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DRIVE_NOT_CLOSED"
    assert db["audits"] == []


def test_other_hr_gets_404(db):
    app.dependency_overrides[security.current_user] = lambda: OTHER_HR
    assert TestClient(app).get(URL).status_code == 404


def test_super_admin_sees_any_drive(db):
    admin = StaffUser(id=uuid.uuid4(), email="a@x.com", full_name="Admin", role="super_admin")
    app.dependency_overrides[security.current_user] = lambda: admin
    assert TestClient(app).get(URL).status_code == 200


def test_no_token_is_401():
    assert TestClient(app).get(URL).status_code == 401

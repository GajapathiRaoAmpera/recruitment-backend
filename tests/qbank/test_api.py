"""Question-bank API: upload, list, detail, questions, edit, approve, replace, delete, download.

No DB, disk or network: the repo, storage, PDF extraction and embeddings are monkeypatched;
parse_pages (pure) runs for real on synthetic page text.
"""

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.auth import security
from app.auth.schemas import StaffUser
from app.core.config import get_settings
from app.core.db import get_session
from app.core.errors import AppError
from app.main import app
from app.qbank import embeddings, parser, repo, service, storage
from app.qbank.parser import SUBJECTS

ADMIN = StaffUser(id=uuid.uuid4(), email="sa@x.com", full_name="S A", role="super_admin")
HR = StaffUser(id=uuid.uuid4(), email="hr@x.com", full_name="H R", role="hr")
HR2 = StaffUser(id=uuid.uuid4(), email="hr2@x.com", full_name="H R2", role="hr")
BASE = "/api/v1/question-banks"
PROG, DSA = SUBJECTS[0], SUBJECTS[1]


def block(n, level, topic, stem) -> str:
    opts = [f"{stem} opt {c}" for c in "ABCD"]
    return "\n".join(
        [f"{n}. {level} - {topic}", stem, *(f"{c}. {o}" for c, o in zip("ABCD", opts, strict=True))]
    )


def bank_pages(tag: str = "v1") -> list[str]:
    """PROG: 3 questions (#3 has a level mismatch -> warning); DSA: 2 questions."""
    q = "\n".join(
        [
            PROG,
            block(1, "Easy", "Basics", f"{tag} prog one?"),
            block(2, "Medium", "Classes", f"{tag} prog two?"),
            block(3, "Easy", "Mismatch", f"{tag} prog three?"),
            DSA,
            block(1, "Hard", "Graphs", f"{tag} dsa one?"),
            block(2, "Easy", "Arrays", f"{tag} dsa two?"),
        ]
    )
    key = "\n".join(
        [
            "Answer Key",
            PROG,
            "# Level Answer",
            "1 Easy B",
            "2 Medium A",
            "3 Medium C",
            DSA,
            "# Level Answer",
            "1 Hard D",
            "2 Easy A",
        ]
    )
    return ["Summary", q, key]


PDF = b"%PDF-1.7\n bank v1"
PDF2 = b"%PDF-1.7\n bank v2"
EMPTY_PDF = b"%PDF-1.7\n nothing"


class Nested:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.savepoints += 1
        return self

    async def __aexit__(self, exc_type, *_):
        if exc_type is not None:
            self.session.savepoint_rollbacks += 1
        return False


class FakeSession:
    def __init__(self):
        self.commits = self.rollbacks = self.savepoints = self.savepoint_rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def begin_nested(self):
        return Nested(self)


class FkError(Exception):
    """Shape of sqlalchemy IntegrityError wrapping asyncpg ForeignKeyViolationError."""

    def __init__(self):
        super().__init__("fk")
        self.orig = type("Orig", (), {"sqlstate": "23503"})()


@pytest.fixture
def db(monkeypatch):
    sources: dict[uuid.UUID, dict] = {}
    questions: dict[uuid.UUID, dict] = {}
    files: dict[str, bytes] = {}
    vectors: dict[uuid.UUID, str] = {}
    audits: list[tuple] = []
    reads: list[tuple] = []  # (source_id, for_update)
    extracted: list[bytes] = []
    embedded: list[dict] = []
    session = FakeSession()
    pages_by_data = {PDF: bank_pages("v1"), PDF2: bank_pages("v2"), EMPTY_PDF: ["no", "questions"]}

    def now():
        return datetime.now(UTC)

    def src_row(s):
        return {k: v for k, v in s.items()}

    async def insert_source(
        _, *, name, filename, storage_key, sha256, track, notes, uploaded_by, report, status="ready"
    ):
        if any(s["sha256"] == sha256 for s in sources.values()):
            raise AssertionError("duplicate hash reached insert")
        sid = uuid.uuid4()
        sources[sid] = dict(
            id=sid,
            name=name,
            filename=filename,
            storage_key=storage_key,
            sha256=sha256,
            track=track,
            notes=notes,
            status=status,
            is_active=True,
            uploaded_by=uploaded_by,
            report=report,
            embedding_status="pending",
            created_at=now(),
            updated_at=now(),
        )
        return src_row(sources[sid])

    async def get_source(_, sid, *, for_update=False):
        reads.append((sid, for_update))
        s = sources.get(sid)
        return None if s is None else src_row(s)

    async def get_source_by_hash(_, sha):
        return next((src_row(s) for s in sources.values() if s["sha256"] == sha), None)

    async def list_sources(_):
        out = []
        for s in sorted(sources.values(), key=lambda s: s["created_at"], reverse=True):
            mine = [q for q in questions.values() if q["source_id"] == s["id"]]
            out.append(
                src_row(s)
                | {"question_count": len(mine), "approved_count": sum(q["approved"] for q in mine)}
            )
        return out

    async def update_source(_, sid, fields):
        if sid not in sources:
            return None
        sources[sid].update(fields, updated_at=now())
        return src_row(sources[sid])

    async def delete_source(_, sid):
        s = sources.pop(sid, None)
        if s is None:
            return None
        for qid in [q for q, v in questions.items() if v["source_id"] == sid]:
            questions.pop(qid)
            vectors.pop(qid, None)
        return {"id": sid, "storage_key": s["storage_key"]}

    async def insert_questions(_, sid, rows):
        for r in rows:
            qid = uuid.uuid4()
            questions[qid] = dict(
                id=qid,
                source_id=sid,
                subject=r["subject"],
                topic=r["topic"],
                level=r["level"],
                text=r["text"],
                options=r["options"],
                answer=r["answer"],
                explanation=None,
                approved=False,
                warnings=r["warnings"],
                source_number=r["number"],
                created_at=now(),
            )
        return len(rows)

    async def delete_questions(_, sid):
        gone = [q for q, v in questions.items() if v["source_id"] == sid]
        for qid in gone:
            questions.pop(qid)
            vectors.pop(qid, None)
        return len(gone)

    async def list_questions(_, sid, *, subject=None, level=None, approved=None, limit, offset):
        rows = [
            q
            for q in questions.values()
            if q["source_id"] == sid
            and subject in (None, q["subject"])
            and level in (None, q["level"])
            and approved in (None, q["approved"])
        ]
        rows.sort(key=lambda q: (q["subject"], q["source_number"]))
        return [dict(q) for q in rows[offset : offset + limit]], len(rows)

    async def counts(_, sid):
        out: dict = {}
        for q in questions.values():
            if q["source_id"] == sid:
                cell = out.setdefault(q["subject"], {}).setdefault(
                    q["level"], {"total": 0, "approved": 0}
                )
                cell["total"] += 1
                cell["approved"] += int(q["approved"])
        return out

    async def get_question(_, qid, *, for_update=False):
        q = questions.get(qid)
        return None if q is None else dict(q)

    async def update_question(_, qid, fields):
        questions[qid].update(fields)
        return dict(questions[qid])

    async def set_approval(_, sid, *, ids=None, subject=None, approved, only_clean=False):
        n = 0
        for q in questions.values():
            if q["source_id"] != sid:
                continue
            if ids is not None and q["id"] not in ids:
                continue
            if subject is not None and q["subject"] != subject:
                continue
            if only_clean and q["warnings"]:
                continue
            q["approved"] = approved
            n += 1
        return n

    async def available_counts(_, track):
        cells: dict = {}
        for q in questions.values():
            s = sources[q["source_id"]]
            if s["track"] == track and s["is_active"] and s["status"] == "ready" and q["approved"]:
                key = (q["subject"], q["level"])
                cells[key] = cells.get(key, 0) + 1
        return [{"subject": k[0], "level": k[1], "n": n} for k, n in sorted(cells.items())]

    async def delete_embedding(_, qid):
        vectors.pop(qid, None)

    for name, fn in dict(
        delete_embedding=delete_embedding,
        insert_source=insert_source,
        get_source=get_source,
        get_source_by_hash=get_source_by_hash,
        list_sources=list_sources,
        update_source=update_source,
        delete_source=delete_source,
        insert_questions=insert_questions,
        delete_questions=delete_questions,
        list_questions=list_questions,
        counts=counts,
        get_question=get_question,
        update_question=update_question,
        set_approval=set_approval,
        available_counts=available_counts,
    ).items():
        monkeypatch.setattr(repo, name, fn)

    async def save(key, data):
        files[key] = data

    async def read(key):
        return files.get(key)

    async def delete(key):
        files.pop(key, None)

    monkeypatch.setattr(storage, "save", save)
    monkeypatch.setattr(storage, "read", read)
    monkeypatch.setattr(storage, "delete", delete)

    def extract_pages(data, *, max_pages=400):
        extracted.append(data)
        if data in pages_by_data:
            return pages_by_data[data]
        raise AppError(422, "PDF_UNREADABLE", "The PDF could not be read")

    monkeypatch.setattr(parser, "extract_pages", extract_pages)

    async def audit(_, actor_id, action, entity, entity_id, diff=None):
        audits.append((actor_id, action, entity_id, diff, session.commits))

    monkeypatch.setattr(service, "audit", audit)

    async def embed_source(source_id, embedder):
        embedded.append({"source_id": source_id, "embedder": embedder, "commits": session.commits})
        return "done"

    async def embed_question(_, qid, embedder):
        if embedder is None:
            vectors.pop(qid, None)
            return False
        vectors[qid] = "fresh:" + questions[qid]["text"]
        return True

    monkeypatch.setattr(embeddings, "embed_source", embed_source)
    monkeypatch.setattr(embeddings, "embed_question", embed_question)
    monkeypatch.setattr(embeddings, "make_embedder", lambda: "EMBEDDER")
    app.dependency_overrides[get_session] = lambda: session
    yield dict(
        sources=sources,
        questions=questions,
        files=files,
        vectors=vectors,
        audits=audits,
        reads=reads,
        extracted=extracted,
        embedded=embedded,
        session=session,
        pages=pages_by_data,
    )
    app.dependency_overrides.clear()


def as_user(user, **kw):
    app.dependency_overrides[security.current_user] = lambda: user
    return TestClient(app, **kw)


def upload(client, data=PDF, name="Core CS", track="it", notes=None, filename="core_cs.pdf"):
    form = {"name": name, "track": track}
    if notes is not None:
        form["notes"] = notes
    return client.post(BASE, data=form, files={"file": (filename, data, "application/pdf")})


def seed(db, user=HR, data=PDF) -> uuid.UUID:
    r = upload(as_user(user), data=data)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


def actions(db):
    return [a[1] for a in db["audits"]]


def qids(db, sid, **match):
    return [
        q["id"]
        for q in db["questions"].values()
        if q["source_id"] == sid and all(q[k] == v for k, v in match.items())
    ]


def err(r):
    return r.json()["error"]


# --- upload ---


def test_upload_parses_and_stores_unapproved(db):
    r = upload(as_user(HR), notes="  from the CS team ")
    assert r.status_code == 201, r.text
    body = r.json()
    sid = uuid.UUID(body["id"])
    assert body["name"] == "Core CS" and body["track"] == "it" and body["filename"] == "core_cs.pdf"
    assert body["notes"] == "from the CS team"
    assert body["question_count"] == 5 and body["approved_count"] == 0
    assert body["embedding_status"] == "pending" and body["status"] == "ready"
    assert body["uploaded_by"] == str(HR.id) and body["can_manage"] is True
    assert body["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert "storage_key" not in body
    src = db["sources"][sid]
    assert db["files"] == {src["storage_key"]: PDF}
    assert src["report"]["total"] == 5 and src["report"]["by_subject"] == {PROG: 3, DSA: 2}
    assert any("Mismatch" in w or "#3" in w for w in src["report"]["warnings"])
    qs = [q for q in db["questions"].values() if q["source_id"] == sid]
    assert len(qs) == 5 and not any(q["approved"] for q in qs)
    assert {q["level"] for q in qs} == {"easy", "medium", "advanced"}
    assert db["session"].commits == 1
    assert actions(db) == ["question_bank.created"]


def test_upload_schedules_embedding_after_commit(db):
    sid = seed(db)
    assert db["embedded"] == [{"source_id": sid, "embedder": "EMBEDDER", "commits": 1}]


def test_upload_rejects_non_pdf_oversize_and_too_many_pages(db, monkeypatch):
    client = as_user(HR)
    r = upload(client, data=b"MZ\x90 not a pdf")
    assert r.status_code == 422 and err(r)["code"] == "NOT_A_PDF"

    monkeypatch.setattr(get_settings(), "qbank_max_mb", 1)
    big = b"%PDF-1.7\n" + b"x" * (1024 * 1024)
    r = upload(client, data=big)
    assert r.status_code == 413 and err(r)["code"] == "FILE_TOO_LARGE"
    assert big not in db["extracted"]  # rejected before any parsing
    monkeypatch.setattr(get_settings(), "qbank_max_mb", 20)

    too_many = b"%PDF-1.7\n many pages"

    def many(data, *, max_pages=400):
        assert max_pages == 400
        raise AppError(422, "PDF_TOO_MANY_PAGES", "too many", {"pages": 401})

    monkeypatch.setattr(parser, "extract_pages", many)
    r = upload(client, data=too_many)
    assert r.status_code == 422 and err(r)["code"] == "PDF_TOO_MANY_PAGES"
    assert db["sources"] == {} and db["files"] == {} and db["audits"] == []


def test_magic_bytes_checked_before_parsing(db):
    r = upload(as_user(HR), data=b"hello %PDF-1.7 later")
    assert r.status_code == 422 and err(r)["code"] == "NOT_A_PDF"
    assert db["extracted"] == []


def test_upload_parse_timeout_is_unreadable(db, monkeypatch):
    import time

    monkeypatch.setattr(service, "PARSE_TIMEOUT_S", 0.05)

    def slow(data, *, max_pages=400):
        time.sleep(0.3)
        return bank_pages()

    monkeypatch.setattr(parser, "extract_pages", slow)
    r = upload(as_user(HR))
    assert r.status_code == 422 and err(r)["code"] == "PDF_UNREADABLE"
    assert err(r)["details"]["reason"] == "timeout"
    assert db["sources"] == {} and db["files"] == {}


def test_upload_validates_form_fields(db):
    client = as_user(HR)
    assert upload(client, track="sales").status_code == 422
    r = upload(client, name="   ")
    assert r.status_code == 422 and err(r)["code"] == "VALIDATION_ERROR"
    assert db["sources"] == {}


def test_duplicate_hash_conflicts(db):
    sid = seed(db)
    r = upload(as_user(HR2), name="Again")
    assert r.status_code == 409
    e = err(r)
    assert e["code"] == "DUPLICATE_FILE"
    assert e["details"] == {"existing_id": str(sid), "existing_name": "Core CS"}
    assert len(db["sources"]) == 1 and len(db["files"]) == 1
    assert db["extracted"] == [PDF]  # the duplicate was not parsed


def test_no_questions_found_saves_nothing(db):
    r = upload(as_user(HR), data=EMPTY_PDF)
    assert r.status_code == 422 and err(r)["code"] == "NO_QUESTIONS_FOUND"
    assert err(r)["details"]["warnings"]
    assert db["sources"] == {} and db["files"] == {} and db["questions"] == {}
    assert db["audits"] == [] and db["session"].commits == 0 and db["embedded"] == []


def test_upload_db_failure_removes_saved_file(db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "insert_questions", boom)
    r = upload(as_user(HR, raise_server_exceptions=False))
    assert r.status_code == 500
    assert db["files"] == {} and db["embedded"] == [] and db["session"].commits == 0


# --- read ---


def test_list_and_detail_counts(db):
    a = seed(db, HR)
    b = seed(db, HR2, data=PDF2)
    for q in qids(db, a, subject=PROG):
        db["questions"][q]["approved"] = True
    client = as_user(HR)
    r = client.get(BASE)
    assert r.status_code == 200
    page = r.json()
    assert page["total"] == 2 and page["page"] == 1 and page["page_size"] == 50
    by_id = {i["id"]: i for i in page["items"]}
    assert by_id[str(a)]["approved_count"] == 3 and by_id[str(a)]["question_count"] == 5
    assert by_id[str(a)]["can_manage"] is True and by_id[str(b)]["can_manage"] is False

    r = client.get(f"{BASE}/{a}")
    assert r.status_code == 200
    d = r.json()
    assert d["counts"][PROG]["easy"] == {"total": 2, "approved": 2}
    assert d["counts"][DSA]["advanced"] == {"total": 1, "approved": 0}
    assert d["report"]["total"] == 5 and d["report"]["by_level"]["advanced"] == 1
    assert d["question_count"] == 5 and d["approved_count"] == 3
    assert d["embedding_status"] == "pending"

    assert client.get(f"{BASE}?page_size=101").status_code == 422
    assert client.get(f"{BASE}?page=2&page_size=1").json()["items"][0]["id"] == str(a)
    r = client.get(f"{BASE}/{uuid.uuid4()}")
    assert r.status_code == 404 and err(r)["code"] == "NOT_FOUND"


def test_questions_pagination_and_filters(db):
    sid = seed(db)
    db["questions"][qids(db, sid, subject=DSA, level="easy")[0]]["approved"] = True
    client = as_user(HR2)  # any HR may view
    r = client.get(f"{BASE}/{sid}/questions?page_size=2&page=1")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5 and len(body["items"]) == 2 and body["page_size"] == 2
    item = body["items"][0]
    assert set(item) >= {"id", "text", "options", "answer", "level", "subject", "warnings"}
    r = client.get(f"{BASE}/{sid}/questions?page=3&page_size=2")
    assert len(r.json()["items"]) == 1
    r = client.get(f"{BASE}/{sid}/questions", params={"subject": DSA, "approved": "true"})
    assert r.json()["total"] == 1 and r.json()["items"][0]["level"] == "easy"
    r = client.get(f"{BASE}/{sid}/questions?level=advanced")
    assert r.json()["total"] == 1
    assert client.get(f"{BASE}/{sid}/questions?level=hard").status_code == 422
    assert client.get(f"{BASE}/{sid}/questions?page_size=101").status_code == 422
    assert client.get(f"{BASE}/{sid}/questions?page=0").status_code == 422
    r = client.get(f"{BASE}/{uuid.uuid4()}/questions")
    assert r.status_code == 404


def test_availability_counts_active_ready_approved(db):
    a = seed(db, HR)
    b = seed(db, HR2, data=PDF2)
    for q in qids(db, a):
        db["questions"][q]["approved"] = True
    for q in qids(db, b, subject=DSA):
        db["questions"][q]["approved"] = True
    db["sources"][b]["is_active"] = False  # hidden from new papers
    client = as_user(HR)
    r = client.get(f"{BASE}/availability?track=it")
    assert r.status_code == 200
    body = r.json()
    assert body["track"] == "it" and body["total"] == 5
    assert body["subjects"][PROG] == {"easy": 2, "medium": 1, "advanced": 0}
    assert body["subjects"][DSA] == {"easy": 1, "medium": 0, "advanced": 1}
    assert body["by_level"] == {"easy": 3, "medium": 1, "advanced": 1}
    r = client.get(f"{BASE}/availability?track=non_it")
    assert r.json() == {
        "track": "non_it",
        "subjects": {},
        "by_level": {"easy": 0, "medium": 0, "advanced": 0},
        "total": 0,
    }
    assert client.get(f"{BASE}/availability?track=sales").status_code == 422
    assert client.get(f"{BASE}/availability").status_code == 422


def test_other_hr_can_view_and_download(db):
    sid = seed(db, HR)
    client = as_user(HR2)
    assert client.get(f"{BASE}/{sid}").json()["can_manage"] is False
    r = client.get(f"{BASE}/{sid}/file")
    assert r.status_code == 200 and r.content == PDF


def test_file_download_headers(db):
    r0 = upload(as_user(HR), filename='..\\evil"name\r\n;.PDF')
    sid = r0.json()["id"]
    r = as_user(HR).get(f"{BASE}/{sid}/file")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;") and "\r" not in cd and "\n" not in cd
    assert '"' not in cd.split("filename=", 1)[1].strip('"')
    assert cd.endswith('.pdf"') and "\\" not in cd
    assert r.headers["x-content-type-options"] == "nosniff"

    db["files"].clear()  # file lost from disk
    r = as_user(HR).get(f"{BASE}/{sid}/file")
    assert r.status_code == 404 and err(r)["code"] == "FILE_NOT_FOUND"
    r = as_user(HR).get(f"{BASE}/{uuid.uuid4()}/file")
    assert r.status_code == 404 and err(r)["code"] == "NOT_FOUND"


# --- metadata ---


def test_patch_metadata_and_deactivate(db):
    sid = seed(db)
    client = as_user(HR)
    r = client.patch(f"{BASE}/{sid}", json={"name": "  CS v2 ", "notes": "n", "track": "non_it"})
    assert r.status_code == 200
    assert r.json()["name"] == "CS v2" and r.json()["track"] == "non_it"
    r = client.patch(f"{BASE}/{sid}", json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert db["sources"][sid]["is_active"] is False
    r = client.patch(f"{BASE}/{sid}", json={"notes": None})
    assert r.status_code == 200 and r.json()["notes"] is None
    assert actions(db).count("question_bank.updated") == 3
    diff = db["audits"][2][3]
    assert diff == {"from": {"is_active": True}, "to": {"is_active": False}}
    for bad in ({}, {"name": ""}, {"name": None}, {"track": "x"}, {"is_active": None}):
        assert client.patch(f"{BASE}/{sid}", json=bad).status_code == 422, bad


# --- replace ---


def test_replace_reparses_and_resets_approval(db):
    sid = seed(db)
    old_key = db["sources"][sid]["storage_key"]
    for q in qids(db, sid):
        db["questions"][q]["approved"] = True
        db["vectors"][q] = "old"
    db["sources"][sid]["embedding_status"] = "done"
    db["embedded"].clear()
    client = as_user(HR)
    r = client.put(f"{BASE}/{sid}/file", files={"file": ("core_v2.pdf", PDF2, "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(sid) and body["filename"] == "core_v2.pdf"
    assert body["approved_count"] == 0 and body["question_count"] == 5
    assert body["embedding_status"] == "pending"
    src = db["sources"][sid]
    assert src["sha256"] == hashlib.sha256(PDF2).hexdigest() and src["storage_key"] != old_key
    assert db["files"] == {src["storage_key"]: PDF2}  # old file removed after commit
    qs = [q for q in db["questions"].values() if q["source_id"] == sid]
    assert len(qs) == 5 and all(q["text"].startswith("v2") for q in qs)
    assert not any(q["approved"] for q in qs) and db["vectors"] == {}
    assert (sid, True) in db["reads"]  # row lock
    assert actions(db)[-1] == "question_bank.replaced"
    assert db["embedded"] == [{"source_id": sid, "embedder": "EMBEDDER", "commits": 2}]


def test_replace_duplicate_hash_conflicts(db):
    a = seed(db, HR)
    b = seed(db, HR, data=PDF2)
    client = as_user(HR)
    r = client.put(f"{BASE}/{a}/file", files={"file": ("x.pdf", PDF2, "application/pdf")})
    assert r.status_code == 409 and err(r)["code"] == "DUPLICATE_FILE"
    assert err(r)["details"]["existing_id"] == str(b)
    r = client.put(f"{BASE}/{a}/file", files={"file": ("x.pdf", PDF, "application/pdf")})
    assert r.status_code == 409 and err(r)["code"] == "DUPLICATE_FILE"
    assert err(r)["details"]["existing_id"] == str(a)


def test_replace_validates_file(db):
    sid = seed(db)
    client = as_user(HR)
    r = client.put(f"{BASE}/{sid}/file", files={"file": ("x.pdf", b"nope", "application/pdf")})
    assert r.status_code == 422 and err(r)["code"] == "NOT_A_PDF"
    r = client.put(f"{BASE}/{sid}/file", files={"file": ("x.pdf", EMPTY_PDF, "application/pdf")})
    assert r.status_code == 422 and err(r)["code"] == "NO_QUESTIONS_FOUND"
    assert len(db["files"]) == 1 and len(qids(db, sid)) == 5


def test_replace_keeps_old_file_if_db_step_fails(db, monkeypatch):
    sid = seed(db)
    old_key = db["sources"][sid]["storage_key"]

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "insert_questions", boom)
    client = as_user(HR, raise_server_exceptions=False)
    r = client.put(f"{BASE}/{sid}/file", files={"file": ("v2.pdf", PDF2, "application/pdf")})
    assert r.status_code == 500
    assert db["files"] == {old_key: PDF}  # new file discarded, old one kept
    assert db["session"].commits == 1  # only the original upload


# --- delete ---


def test_delete_removes_file_and_rows(db):
    sid = seed(db)
    other = seed(db, data=PDF2)
    r = as_user(HR).delete(f"{BASE}/{sid}")
    assert r.status_code == 204 and r.content == b""
    assert sid not in db["sources"] and not qids(db, sid)
    assert len(db["files"]) == 1 and other in db["sources"]
    assert (sid, True) in db["reads"]
    assert actions(db)[-1] == "question_bank.deleted"
    assert as_user(HR).delete(f"{BASE}/{sid}").status_code == 404


def test_delete_fk_violation_is_bank_in_use(db, monkeypatch):
    sid = seed(db)

    async def fk(*a, **k):
        raise FkError()

    monkeypatch.setattr(repo, "delete_source", fk)
    r = as_user(HR).delete(f"{BASE}/{sid}")
    assert r.status_code == 409 and err(r)["code"] == "BANK_IN_USE"
    assert "deactivate" in err(r)["message"].lower()
    assert sid in db["sources"] and len(db["files"]) == 1
    assert db["session"].commits == 1


# --- questions ---


def edit_body(**kw):
    return {
        "text": " Edited stem? ",
        "options": ["one", "two", "three", "four"],
        "answer": 2,
        "why": " because ",
    } | kw


def test_edit_question_validates_and_audits(db):
    sid = seed(db)
    qid = qids(db, sid)[0]
    db["vectors"][qid] = "stale"
    client = as_user(HR)
    r = client.patch(f"{BASE}/{sid}/questions/{qid}", json=edit_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "Edited stem?" and body["answer"] == 2
    assert body["options"] == ["one", "two", "three", "four"] and body["explanation"] == "because"
    assert db["vectors"][qid] == "fresh:Edited stem?"
    a = db["audits"][-1]
    assert a[1] == "question_bank.question_edited" and a[2] == sid
    assert a[3]["question_id"] == str(qid) and a[3]["to"]["text"] == "Edited stem?"
    for bad in (
        edit_body(options=["a", "a", "b", "c"]),
        edit_body(answer=4),
        edit_body(text=" "),
        edit_body(options=["a", "b", "c"]),
    ):
        assert client.patch(f"{BASE}/{sid}/questions/{qid}", json=bad).status_code == 422
    other = seed(db, data=PDF2)
    foreign = qids(db, other)[0]
    r = client.patch(f"{BASE}/{sid}/questions/{foreign}", json=edit_body())
    assert r.status_code == 404 and err(r)["code"] == "QUESTION_NOT_FOUND"


def test_embedding_failure_never_blocks_edit(db, monkeypatch):
    sid = seed(db)
    qid = qids(db, sid)[0]
    db["vectors"][qid] = "stale"

    async def flaky(session, q, embedder):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(embeddings, "embed_question", flaky)
    r = as_user(HR).patch(f"{BASE}/{sid}/questions/{qid}", json=edit_body())
    assert r.status_code == 200
    assert db["questions"][qid]["text"] == "Edited stem?"
    assert qid not in db["vectors"]  # stale vector removed, never kept
    assert db["session"].savepoint_rollbacks == 1 and db["session"].commits == 2

    def no_key():
        raise RuntimeError("config")

    monkeypatch.setattr(embeddings, "make_embedder", no_key)
    assert as_user(HR).patch(f"{BASE}/{sid}/questions/{qid}", json=edit_body()).status_code == 200


def test_approval_scopes_all_clean_subject_ids(db):
    sid = seed(db)
    client = as_user(HR)
    url = f"{BASE}/{sid}/approval"
    r = client.put(url, json={"approved": True, "scope": "all_clean"})
    assert r.status_code == 200
    assert r.json() == {"changed": 4, "question_count": 5, "approved_count": 4}  # #3 has a warning
    assert (sid, True) in db["reads"]

    r = client.put(url, json={"approved": False, "scope": "subject", "subject": DSA})
    assert r.json() == {"changed": 2, "question_count": 5, "approved_count": 2}

    ids = qids(db, sid, subject=DSA)
    r = client.put(url, json={"approved": True, "scope": "ids", "question_ids": [str(ids[0])]})
    assert r.json() == {"changed": 1, "question_count": 5, "approved_count": 3}

    r = client.put(url, json={"approved": False, "scope": "all_clean"})
    assert r.json()["approved_count"] == 0  # unapprove covers every question

    r = client.put(url, json={"approved": True, "scope": "subject", "subject": "Nope"})
    assert r.status_code == 422 and err(r)["code"] == "UNKNOWN_SUBJECT"
    r = client.put(
        url, json={"approved": True, "scope": "ids", "question_ids": [str(uuid.uuid4())]}
    )
    assert r.status_code == 422 and err(r)["code"] == "UNKNOWN_QUESTIONS"
    for bad in (
        {"approved": True, "scope": "subject"},
        {"approved": True, "scope": "ids", "question_ids": []},
        {"approved": True, "scope": "all_clean", "subject": DSA},
        {"approved": True, "scope": "everything"},
    ):
        assert client.put(url, json=bad).status_code == 422, bad
    assert actions(db).count("question_bank.approval_changed") == 4


# --- permissions ---


@pytest.mark.parametrize(
    "method,path,kw",
    [
        ("patch", "", {"json": {"name": "x"}}),
        ("put", "/file", {"files": {"file": ("v2.pdf", PDF2, "application/pdf")}}),
        ("delete", "", {}),
        ("put", "/approval", {"json": {"approved": True, "scope": "all_clean"}}),
        ("patch", "/questions/{qid}", {"json": edit_body()}),
    ],
)
def test_only_uploader_or_super_admin_mutates(db, method, path, kw):
    sid = seed(db, HR)
    qid = qids(db, sid)[0]
    url = f"{BASE}/{sid}{path.format(qid=qid)}"
    before = list(db["audits"])
    r = getattr(as_user(HR2), method)(url, **kw)
    assert r.status_code == 403 and err(r)["code"] == "FORBIDDEN"
    assert db["audits"] == before and sid in db["sources"]
    r = getattr(as_user(HR2), method)(f"{BASE}/{uuid.uuid4()}{path.format(qid=qid)}", **kw)
    assert r.status_code == 404 and err(r)["code"] == "NOT_FOUND"
    r = getattr(as_user(ADMIN), method)(url, **kw)
    assert r.status_code in (200, 204), r.text


def test_every_mutation_audited(db):
    sid = seed(db)
    qid = qids(db, sid)[0]
    client = as_user(HR)
    steps = [
        lambda: client.patch(f"{BASE}/{sid}", json={"name": "y"}),
        lambda: client.patch(f"{BASE}/{sid}/questions/{qid}", json=edit_body()),
        lambda: client.put(f"{BASE}/{sid}/approval", json={"approved": True, "scope": "all_clean"}),
        lambda: client.put(
            f"{BASE}/{sid}/file", files={"file": ("v2.pdf", PDF2, "application/pdf")}
        ),
        lambda: client.delete(f"{BASE}/{sid}"),
    ]
    for step in steps:
        assert step().status_code in (200, 204)
    assert actions(db) == [
        "question_bank.created",
        "question_bank.updated",
        "question_bank.question_edited",
        "question_bank.approval_changed",
        "question_bank.replaced",
        "question_bank.deleted",
    ]
    # each audit row was written before its transaction's commit
    assert [a[4] for a in db["audits"]] == [0, 1, 2, 3, 4, 5]
    assert db["session"].commits == 6
    assert all(a[0] == HR.id for a in db["audits"])


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", BASE),
        ("post", BASE),
        ("get", f"{BASE}/availability?track=it"),
        ("get", f"{BASE}/{uuid.uuid4()}"),
        ("get", f"{BASE}/{uuid.uuid4()}/questions"),
        ("get", f"{BASE}/{uuid.uuid4()}/file"),
        ("patch", f"{BASE}/{uuid.uuid4()}"),
        ("put", f"{BASE}/{uuid.uuid4()}/file"),
        ("delete", f"{BASE}/{uuid.uuid4()}"),
        ("patch", f"{BASE}/{uuid.uuid4()}/questions/{uuid.uuid4()}"),
        ("put", f"{BASE}/{uuid.uuid4()}/approval"),
    ],
)
def test_unauthenticated_401_and_non_staff_403(db, monkeypatch, method, path):
    app.dependency_overrides.pop(security.current_user, None)
    client = TestClient(app)
    r = client.request(method.upper(), path)
    assert r.status_code == 401 and err(r)["code"] == "UNAUTHENTICATED"

    async def no_user(_, __):
        return None

    async def verified(_):
        return uuid.uuid4()

    monkeypatch.setattr(security.repo, "get_user", no_user)
    monkeypatch.setattr(security, "verify_token", verified)
    r = client.request(method.upper(), path, headers={"Authorization": "Bearer t"})
    assert r.status_code == 403 and err(r)["code"] == "NOT_STAFF"

"""retrieve_technical: per level / subject allocation, filters, focus ranking, shortfall.
repo.search_questions is replaced by an in-memory equivalent; one test checks the real SQL's
filters with a recording session. No network, no DB."""

import asyncio
import math
import uuid
from collections import Counter

import pytest

from app.qbank import repo, retrieval
from app.qbank.retrieval import BankQuestion, Retrieval, retrieve_technical

OS, DB, NET = "Operating Systems", "Database Management Systems", "Computer Networks"


def run(coro):
    return asyncio.run(coro)


def cosine_distance(a, b):
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return 1 - dot / (math.hypot(*a) * math.hypot(*b))


class Bank:
    """In-memory stand-in for the questions + sources + embeddings tables."""

    def __init__(self):
        self.rows: list[dict] = []
        self.calls: list[dict] = []
        self._n = 0

    def add(
        self,
        subject,
        level,
        count=1,
        *,
        track="it",
        approved=True,
        active=True,
        status="ready",
        emb=None,
        model="fake-embed",
        text=None,
    ):
        for _ in range(count):
            self._n += 1
            self.rows.append(
                {
                    "id": uuid.UUID(int=self._n),
                    "source_id": uuid.UUID(int=999),
                    "subject": subject,
                    "topic": f"t{self._n}",
                    "level": level,
                    "text": text or f"{subject} {level} #{self._n}",
                    "options": ["a", "b", "c", "d"],
                    "answer": 1,
                    "explanation": "because",
                    "_track": track,
                    "_approved": approved,
                    "_active": active,
                    "_status": status,
                    "_emb": emb,
                    "_model": model,
                    "_order": self._n,
                }
            )
        return self

    async def search(
        self, session, *, track, levels, subjects=None, query_vec=None, per_cell, model=None
    ):
        self.calls.append(
            {
                "track": track,
                "levels": list(levels),
                "subjects": subjects,
                "per_cell": per_cell,
                "query_vec": query_vec,
                "model": model,
            }
        )
        ok = [
            r
            for r in self.rows
            if r["_track"] == track
            and r["_active"]
            and r["_status"] == "ready"
            and r["_approved"]
            and r["level"] in levels
            and (subjects is None or r["subject"] in subjects)
        ]

        def dist(r):
            if query_vec is None or r["_emb"] is None or (model and r["_model"] != model):
                return None
            return cosine_distance(r["_emb"], query_vec)

        cells: dict = {}
        for r in ok:
            cells.setdefault((r["subject"], r["level"]), []).append({**r, "distance": dist(r)})
        out = []
        for key in sorted(cells):
            ranked = sorted(
                cells[key],
                key=lambda r: (r["distance"] is None, r["distance"] or 0.0, r["_order"]),
            )
            out.extend(ranked[:per_cell])
        return out


@pytest.fixture
def bank(monkeypatch):
    b = Bank()
    monkeypatch.setattr(retrieval.repo, "search_questions", b.search)
    return b


class FakeEmbedder:
    model = "fake-embed"

    def __init__(self, vector=(1.0, 0.0), fail=None):
        self.vector = list(vector)
        self.fail = fail
        self.calls: list = []

    async def embed(self, texts, *, task):
        self.calls.append((list(texts), task))
        if self.fail:
            raise self.fail
        return [list(self.vector) for _ in texts]


def cells(result: Retrieval) -> Counter:
    return Counter((q.level, q.subject) for q in result.questions)


def test_retrieve_respects_level_subject_counts(bank):
    for s in (OS, DB, NET):
        for lv in ("easy", "medium", "advanced"):
            bank.add(s, lv, 10)
    res = run(
        retrieve_technical(
            None,
            track="it",
            per_level={"easy": 5, "medium": 3, "advanced": 0},
            subjects={OS: 50, DB: 30, NET: 20},
            focus=None,
            embedder=None,
        )
    )
    # easy 5 by 50/30/20 -> 2.5/1.5/1.0 -> largest remainder: OS 3, DB 1, NET 1 (tie -> key order)
    # medium 3 -> 1.5/0.9/0.6 -> OS 1, DB 1, NET 1
    assert cells(res) == Counter(
        {
            ("easy", OS): 3,
            ("easy", DB): 1,
            ("easy", NET): 1,
            ("medium", OS): 1,
            ("medium", DB): 1,
            ("medium", NET): 1,
        }
    )
    assert res.shortfall == {}
    assert res.focus_applied is False
    assert all(isinstance(q, BankQuestion) for q in res.questions)
    q = res.questions[0]
    assert (q.level, q.subject, q.answer, q.why, q.options) == (
        "easy",
        OS,
        1,
        "because",
        ["a", "b", "c", "d"],
    )
    assert q.id == str(bank.rows[0]["id"])
    # deterministic: same call twice -> same ids in the same order
    again = run(
        retrieve_technical(
            None,
            track="it",
            per_level={"easy": 5, "medium": 3, "advanced": 0},
            subjects={OS: 50, DB: 30, NET: 20},
            focus=None,
            embedder=None,
        )
    )
    assert [q.id for q in again.questions] == [q.id for q in res.questions]
    # only the weighted subjects are searched, and never more than a level needs per cell
    assert bank.calls[0]["subjects"] == [OS, DB, NET]
    assert bank.calls[0]["per_cell"] == 5
    assert bank.calls[0]["levels"] == ["easy", "medium"]


def test_equal_split_when_subjects_none(bank):
    for s in (OS, DB, NET):
        bank.add(s, "medium", 5)
    res = run(
        retrieve_technical(
            None,
            track="it",
            per_level={"easy": 0, "medium": 6, "advanced": 0},
            subjects=None,
            focus=None,
            embedder=None,
        )
    )
    assert cells(res) == Counter({("medium", OS): 2, ("medium", DB): 2, ("medium", NET): 2})
    assert bank.calls[0]["subjects"] is None


def test_retrieve_excludes_unapproved_inactive_other_track(bank):
    bank.add(OS, "easy", 2, approved=False)
    bank.add(OS, "easy", 2, active=False)
    bank.add(OS, "easy", 2, track="non_it")
    bank.add(OS, "easy", 2, status="processing")
    good = bank.add(OS, "easy", 1).rows[-1]["id"]
    res = run(
        retrieve_technical(
            None, track="it", per_level={"easy": 3}, subjects=None, focus=None, embedder=None
        )
    )
    assert [q.id for q in res.questions] == [str(good)]
    assert res.shortfall == {"easy/Operating Systems": 2}
    assert bank.calls[0]["track"] == "it"


def test_search_sql_filters_approved_active_ready_track():
    """The real repo query carries the filters retrieval relies on."""

    class Rec:
        def __init__(self):
            self.calls = []

        async def execute(self, stmt, params=None):
            self.calls.append((" ".join(str(stmt).split()), params))

            class R:
                def mappings(self):
                    return self

                def all(self):
                    return []

            return R()

    s = Rec()
    run(repo.search_questions(s, track="it", levels=["easy"], per_cell=3))
    sql, params = s.calls[0]
    for cond in ("s.track = :track", "s.is_active", "s.status = 'ready'", "q.approved"):
        assert cond in sql
    assert params["track"] == "it"
    # no focus: deterministic order (upload time, then id) and no embeddings join
    assert "order by s.created_at, q.id" in sql
    assert "question_embeddings" not in sql

    # focus path: LEFT JOIN embeddings with the model in the ON clause (an inner join or a
    # WHERE condition would drop questions without a vector), cosine operator, nulls last
    s = Rec()
    run(
        repo.search_questions(
            s, track="it", levels=["easy"], per_cell=3, query_vec=[1.0, 0.0], model="m-1"
        )
    )
    sql, params = s.calls[0]
    assert (
        "left join question_embeddings e on e.question_id = q.id and e.model = :model where " in sql
    )
    where = sql.split(" where ", 1)[1]
    assert "e.model" not in where.split(") ranked")[0]
    assert "(e.embedding <=> cast(:query_vec as vector)) asc nulls last" in sql
    assert params["model"] == "m-1" and params["query_vec"] == "[1.0,0.0]"


def test_subject_without_stock_is_redistributed(bank):
    bank.add(OS, "easy", 10).add(DB, "easy", 10)  # no NET questions at all
    res = run(
        retrieve_technical(
            None,
            track="it",
            per_level={"easy": 6},
            subjects={OS: 1, DB: 1, NET: 1},
            focus=None,
            embedder=None,
        )
    )
    assert cells(res) == Counter({("easy", OS): 3, ("easy", DB): 3})
    assert res.shortfall == {}


def test_partial_stock_redistributed_by_weight(bank):
    bank.add(OS, "medium", 1).add(DB, "medium", 10).add(NET, "medium", 10)
    res = run(
        retrieve_technical(
            None,
            track="it",
            per_level={"medium": 9},
            subjects={OS: 60, DB: 20, NET: 20},
            focus=None,
            embedder=None,
        )
    )
    # shares of 9 by 60/20/20 = 5.4/1.8/1.8 -> floors 5/1/1, the 2 left go to the biggest
    # remainders (DB .8, NET .8) -> 5/2/2. OS has 1 question, so its 4 uncovered go to DB/NET
    # by their equal weights (2 each) -> 1/4/4.
    assert cells(res) == Counter({("medium", OS): 1, ("medium", DB): 4, ("medium", NET): 4})
    assert res.shortfall == {}


def test_shortfall_reported_not_invented(bank):
    bank.add(OS, "advanced", 2).add(DB, "advanced", 1)
    res = run(
        retrieve_technical(
            None,
            track="it",
            per_level={"easy": 2, "advanced": 6},
            subjects={OS: 1, DB: 1},
            focus=None,
            embedder=None,
        )
    )
    assert len(res.questions) == 3  # everything there is, nothing made up
    assert {q.id for q in res.questions} == {str(r["id"]) for r in bank.rows}
    assert sum(res.shortfall.values()) == 2 + 3
    assert res.shortfall["easy/Operating Systems"] == 1
    assert res.shortfall["easy/Database Management Systems"] == 1
    assert res.shortfall == {
        "easy/Operating Systems": 1,
        "easy/Database Management Systems": 1,
        "advanced/Operating Systems": 1,
        "advanced/Database Management Systems": 2,
    }


def test_shortfall_with_empty_bank_and_no_subjects(bank):
    res = run(
        retrieve_technical(
            None,
            track="it",
            per_level={"easy": 2, "medium": 0, "advanced": 1},
            subjects=None,
            focus=None,
            embedder=None,
        )
    )
    assert res.questions == []
    assert res.shortfall == {"easy/*": 2, "advanced/*": 1}


def test_focus_ranks_by_similarity_when_embeddings_exist(bank):
    bank.add(OS, "easy", 1, emb=[0.0, 1.0], text="far")
    bank.add(OS, "easy", 1, emb=[0.7, 0.7], text="middle")
    bank.add(OS, "easy", 1, emb=[1.0, 0.05], text="near")
    bank.add(OS, "easy", 1, emb=None, text="no vector")
    fake = FakeEmbedder(vector=(1.0, 0.0))
    res = run(
        retrieve_technical(
            None,
            track="it",
            per_level={"easy": 2},
            subjects=None,
            focus="  process scheduling ",
            embedder=fake,
        )
    )
    assert [q.text for q in res.questions] == ["near", "middle"]
    assert res.focus_applied is True and res.focus_partial is False
    assert (res.ranked, res.unranked) == (2, 0)
    assert fake.calls == [(["process scheduling"], "query")]
    assert bank.calls[0]["query_vec"] == [1.0, 0.0]
    assert bank.calls[0]["model"] == "fake-embed"


def test_focus_ignored_with_explicit_flag_when_no_embeddings(bank):
    bank.add(OS, "easy", 3)  # no embeddings stored
    fake = FakeEmbedder()
    res = run(
        retrieve_technical(
            None,
            track="it",
            per_level={"easy": 2},
            subjects=None,
            focus="scheduling",
            embedder=fake,
        )
    )
    assert res.focus_applied is False and res.focus_partial is False
    assert (res.ranked, res.unranked) == (0, 2)
    assert [q.text for q in res.questions] == [r["text"] for r in bank.rows[:2]]


def test_mixed_banks_focus_is_partial_not_applied(bank):
    # bank A embedded, bank B's embedding failed/skipped (no vectors)
    bank.add(OS, "easy", 2, emb=[1.0, 0.0], text="A")
    bank.add(OS, "easy", 2, emb=None, text="B")
    bank.add(DB, "easy", 2, emb=None, text="B")
    res = run(
        retrieve_technical(
            None,
            track="it",
            per_level={"easy": 6},
            subjects=None,
            focus="scheduling",
            embedder=FakeEmbedder(),
        )
    )
    assert len(res.questions) == 6
    assert (res.ranked, res.unranked) == (2, 4)
    assert res.focus_applied is False and res.focus_partial is True
    # ranked ones first inside their cell
    assert [q.text for q in res.questions if q.subject == OS] == ["A", "A", "B", "B"]


def test_without_focus_counts_are_zero(bank):
    bank.add(OS, "easy", 2, emb=[1.0, 0.0])
    res = run(
        retrieve_technical(
            None, track="it", per_level={"easy": 2}, subjects=None, focus=None, embedder=None
        )
    )
    assert (res.ranked, res.unranked, res.focus_applied, res.focus_partial) == (0, 0, False, False)


def test_focus_ignored_without_embedder_or_on_embed_failure(bank):
    bank.add(OS, "easy", 2, emb=[1.0, 0.0])
    for emb in (None, FakeEmbedder(fail=RuntimeError("down"))):
        res = run(
            retrieve_technical(
                None,
                track="it",
                per_level={"easy": 1},
                subjects=None,
                focus="scheduling",
                embedder=emb,
            )
        )
        assert res.focus_applied is False
        assert len(res.questions) == 1
        assert bank.calls[-1]["query_vec"] is None


def test_blank_focus_not_embedded(bank):
    bank.add(OS, "easy", 1, emb=[1.0, 0.0])
    fake = FakeEmbedder()
    res = run(
        retrieve_technical(
            None, track="it", per_level={"easy": 1}, subjects=None, focus="   ", embedder=fake
        )
    )
    assert fake.calls == [] and res.focus_applied is False


def test_nothing_requested_makes_no_query(bank):
    res = run(
        retrieve_technical(
            None, track="it", per_level={"easy": 0}, subjects=None, focus=None, embedder=None
        )
    )
    assert res == Retrieval(questions=[], shortfall={}, focus_applied=False)
    assert bank.calls == []


@pytest.mark.parametrize(
    "per_level, subjects",
    [
        ({"easy": -1}, None),
        ({"hard": 1}, None),
        ({"easy": 1}, {OS: -1}),
        ({"easy": 1}, {OS: 0}),
        ({"easy": 1}, {}),
    ],
)
def test_bad_arguments_rejected(bank, per_level, subjects):
    with pytest.raises(ValueError):
        run(
            retrieve_technical(
                None,
                track="it",
                per_level=per_level,
                subjects=subjects,
                focus=None,
                embedder=None,
            )
        )

import pytest

from app.papers.builder import (
    build_paper,
    needed,
    overlap,
    run_checks,
    split_by_percent,
)
from app.papers.schemas import LEVELS, SECTIONS, PaperSettings, PoolQuestion


def make_settings(**kw) -> PaperSettings:
    base = {
        "track": "it",
        "counts": {"easy": 4, "medium": 4, "advanced": 2},
        "mix": {"aptitude": 40, "reasoning": 30, "technical": 30},
    }
    base.update(kw)
    return PaperSettings(**base)


def make_pool(per_cell: int = 8) -> list[PoolQuestion]:
    pool = []
    for level in LEVELS:
        for section in SECTIONS:
            for i in range(per_cell):
                pool.append(
                    PoolQuestion(
                        id=f"{level}-{section}-{i}",
                        level=level,
                        section=section,
                        topic="t",
                        style="concept",
                        text=f"Question {level} {section} {i}?",
                        options=[f"a{i}", f"b{i}", f"c{i}", f"d{i}"],
                        answer=i % 4,
                        why="Because.",
                    )
                )
    return pool


@pytest.mark.parametrize(
    ("total", "weights"),
    [
        (1, {"a": 34, "b": 33, "c": 33}),
        (10, {"a": 40, "b": 30, "c": 30}),
        (7, {"a": 50, "b": 25, "c": 25}),
        (0, {"a": 50, "b": 50}),
        (13, {"a": 1, "b": 1, "c": 1}),
    ],
)
def test_split_sums_to_total(total, weights):
    out = split_by_percent(total, weights)
    assert sum(out.values()) == total
    assert set(out) == set(weights)
    assert all(v >= 0 for v in out.values())


def test_split_one_question_goes_to_largest_remainder():
    assert split_by_percent(1, {"a": 34, "b": 33, "c": 33}) == {"a": 1, "b": 0, "c": 0}


def test_same_candidate_same_paper():
    s, pool = make_settings(), make_pool()
    a = build_paper(s, "d1", "c1", pool)
    b = build_paper(s, "d1", "c1", pool)
    assert a.items == b.items


def test_different_candidates_differ():
    s, pool = make_settings(), make_pool()
    a = build_paper(s, "d1", "c1", pool)
    b = build_paper(s, "d1", "c2", pool)
    assert [i.id for i in a.items] != [i.id for i in b.items]


def test_fixed_mode_same_questions_for_all():
    s, pool = make_settings(different_per_student=False), make_pool()
    a = build_paper(s, "d1", "c1", pool)
    b = build_paper(s, "d1", "c2", pool)
    assert a.items == b.items


def test_no_repeated_question():
    s = make_settings()
    paper = build_paper(s, "d1", "c1", make_pool())
    ids = [i.id for i in paper.items]
    assert len(ids) == len(set(ids))


def test_no_repeated_text_even_with_different_ids():
    s = make_settings(counts={"easy": 2, "medium": 0, "advanced": 0})
    pool = make_pool(per_cell=1)
    dup = pool[0].model_copy(update={"id": "dup"})
    paper = build_paper(s, "d1", "c1", [*pool, dup])
    texts = [i.text for i in paper.items]
    assert len(texts) == len(set(texts))


def test_counts_per_level_and_section_match_settings():
    s = make_settings()
    paper = build_paper(s, "d1", "c1", make_pool())
    want = needed(s)
    assert paper.missing == {}
    for level in LEVELS:
        for section in SECTIONS:
            got = sum(1 for i in paper.items if i.level == level and i.section == section)
            assert got == want[level][section]
    assert len(paper.items) == sum(s.counts.values())


def test_shortfall_reported_not_invented():
    s = make_settings()
    pool = [q for q in make_pool() if not (q.level == "medium" and q.section == "technical")]
    paper = build_paper(s, "d1", "c1", pool)
    want = needed(s)["medium"]["technical"]
    assert want > 0
    assert paper.missing == {"medium/technical": want}
    assert {i.id for i in paper.items} <= {q.id for q in pool}
    assert len(paper.items) == sum(s.counts.values()) - want


def test_fixed_options_never_shuffled():
    s = make_settings()
    pool = [q.model_copy(update={"fixed_options": True}) for q in make_pool()]
    by_id = {q.id: q for q in pool}
    for cand in ("c1", "c2", "c3"):
        for item in build_paper(s, "d1", cand, pool).items:
            assert item.options == by_id[item.id].options
            assert item.answer == by_id[item.id].answer


def test_answer_index_follows_option_shuffle():
    s = make_settings()
    pool = make_pool()
    by_id = {q.id: q for q in pool}
    moved = 0
    for cand in ("c1", "c2", "c3"):
        for item in build_paper(s, "d1", cand, pool).items:
            src = by_id[item.id]
            assert item.options[item.answer] == src.options[src.answer]
            assert sorted(item.options) == sorted(src.options)
            moved += item.options != src.options
    assert moved > 0


def test_no_shuffle_keeps_order_and_options():
    s = make_settings(shuffle_options=False, shuffle_questions=False)
    pool = make_pool()
    by_id = {q.id: q for q in pool}
    paper = build_paper(s, "d1", "c1", pool)
    assert all(i.options == by_id[i.id].options for i in paper.items)
    levels = [LEVELS.index(i.level) for i in paper.items]
    assert levels == sorted(levels)


def test_edited_question_appears_edited_in_paper():
    s = make_settings(counts={"easy": 3, "medium": 0, "advanced": 0}, different_per_student=False)
    pool = make_pool(per_cell=1)
    pool[0] = pool[0].model_copy(
        update={
            "text": "Edited text?",
            "options": ["w", "x", "y", "z"],
            "answer": 2,
            "edited": True,
        }
    )
    paper = build_paper(s, "d1", "sample", pool)
    item = next(i for i in paper.items if i.id == pool[0].id)
    assert item.text == "Edited text?"
    assert item.edited is True
    assert item.options[item.answer] == "y"


def test_overlap_counts():
    s, pool = make_settings(), make_pool()
    a = build_paper(s, "d1", "c1", pool).items
    assert overlap(a, a).shared == len(a)
    assert overlap(a, a).same_position == len(a)
    assert overlap(a, a).total == len(a)
    assert overlap(a, list(reversed(a))).shared == len(a)
    assert overlap(a, []).shared == 0


def test_pool_covers_check_fails_on_shortfall():
    s = make_settings()
    pool = [q for q in make_pool() if q.section != "reasoning"]
    checks = {c.id: c for c in run_checks(s, "d1", pool)}
    assert checks["pool_covers"].ok is False
    assert "reasoning" in checks["pool_covers"].detail


def test_all_checks_pass_on_good_pool():
    checks = run_checks(make_settings(), "d1", make_pool())
    assert [c.id for c in checks] == [
        "settings_valid",
        "pool_covers",
        "no_repeats",
        "answers_present",
        "students_differ",
    ]
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_no_repeats_check_flags_duplicate_text():
    pool = make_pool()
    pool.append(pool[0].model_copy(update={"id": "dup", "text": f"  {pool[0].text.upper()} "}))
    checks = {c.id: c for c in run_checks(make_settings(), "d1", pool)}
    assert checks["no_repeats"].ok is False


def test_answers_present_check_flags_bad_question():
    for bad in (
        {"options": ["a", "b", "c"]},
        {"options": ["a", "a", "b", "c"]},
        {"answer": 4},
        {"answer": -1},
        {"why": "  "},
    ):
        pool = make_pool()
        pool[3] = pool[3].model_copy(update=bad)
        checks = {c.id: c for c in run_checks(make_settings(), "d1", pool)}
        assert checks["answers_present"].ok is False, bad
        assert pool[3].id in checks["answers_present"].detail


def _easy_only(**kw):
    return make_settings(counts={"easy": 3, "medium": 0, "advanced": 0}, **kw)


def test_students_differ_fails_when_pool_equals_paper_even_with_shuffling():
    pool = [q for q in make_pool(per_cell=1) if q.level == "easy"]
    checks = {c.id: c for c in run_checks(_easy_only(), "d1", pool)}
    assert checks["students_differ"].ok is False
    assert "3/3" in checks["students_differ"].detail
    assert "pool_factor" in checks["students_differ"].detail


def test_students_differ_passes_with_pool_twice_paper_size():
    pool = [q for q in make_pool(per_cell=2) if q.level == "easy"]
    checks = {c.id: c for c in run_checks(_easy_only(), "d1", pool)}
    assert checks["students_differ"].ok is True


def test_students_differ_always_passes_in_fixed_mode():
    pool = [q for q in make_pool(per_cell=1) if q.level == "easy"]
    s = _easy_only(different_per_student=False)
    checks = {c.id: c for c in run_checks(s, "d1", pool)}
    assert checks["students_differ"].ok is True


def test_draw_dedupes_by_id_even_with_different_text():
    s = make_settings(
        counts={"easy": 2, "medium": 0, "advanced": 0},
        mix={"aptitude": 100, "reasoning": 0, "technical": 0},
        different_per_student=False,
    )
    original = next(q for q in make_pool() if q.level == "easy" and q.section == "aptitude")
    clone = original.model_copy(update={"text": "Totally different text?"})
    paper = build_paper(s, "d1", "c1", [original, clone])
    assert [i.id for i in paper.items] == [original.id]
    assert paper.missing == {"easy/aptitude": 1}

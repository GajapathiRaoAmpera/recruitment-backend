import pytest
from pydantic import ValidationError

from app.papers.schemas import KeyItem, PaperItem, PaperSettings, QuestionEdit


def make(**over):
    base = {
        "track": "it",
        "counts": {"easy": 5, "medium": 5, "advanced": 5},
        "mix": {"aptitude": 30, "reasoning": 30, "technical": 40},
    }
    base.update(over)
    return PaperSettings(**base)


def test_valid_defaults():
    s = make()
    assert s.pool_factor == 2 and s.duration_min == 60 and s.it_syntax_percent == 50


def test_role_is_optional_and_normalised():
    assert make().role is None  # drafts saved before the field existed still load
    assert make(role="  Java   Backend\nDeveloper ").role == "Java Backend Developer"
    assert make(role="   ").role is None
    # settings of older drafts carry the removed question-bank keys: ignored, not an error
    assert make(subjects={"Operating Systems": 100}, focus="scheduling").role is None
    with pytest.raises(ValidationError):
        make(role="x" * 121)


def test_mix_must_sum_to_100():
    with pytest.raises(ValidationError):
        make(mix={"aptitude": 30, "reasoning": 30, "technical": 30})


def test_counts_total_must_be_positive():
    with pytest.raises(ValidationError):
        make(counts={"easy": 0, "medium": 0, "advanced": 0})


def test_negative_count_rejected():
    with pytest.raises(ValidationError):
        make(counts={"easy": -1, "medium": 5, "advanced": 5})
    with pytest.raises(ValidationError):
        make(mix={"aptitude": -10, "reasoning": 60, "technical": 50})


def test_all_levels_and_sections_required():
    with pytest.raises(ValidationError):
        make(counts={"easy": 5, "medium": 5})
    with pytest.raises(ValidationError):
        make(mix={"aptitude": 50, "reasoning": 50})
    with pytest.raises(ValidationError):
        make(mix={"aptitude": 50, "reasoning": 50, "technical": 0, "bogus": 0})


def test_pool_factor_and_duration_bounds():
    for bad in ({"pool_factor": 0}, {"pool_factor": 4}, {"duration_min": 4}, {"duration_min": 241}):
        with pytest.raises(ValidationError):
            make(**bad)
    make(pool_factor=3, duration_min=240)
    make(pool_factor=1, duration_min=5)


def edit(**over):
    base = {"text": "Q?", "options": ["a", "b", "c", "d"], "answer": 1, "why": "because"}
    base.update(over)
    return QuestionEdit(**base)


def test_question_edit_rejects_duplicate_options():
    with pytest.raises(ValidationError):
        edit(options=["a", "b", "B ", "d"])


def test_question_edit_rejects_bad_answer_index():
    for bad in (-1, 4):
        with pytest.raises(ValidationError):
            edit(answer=bad)


def test_question_edit_rejects_blank_text():
    with pytest.raises(ValidationError):
        edit(text="   ")
    with pytest.raises(ValidationError):
        edit(why=" ")
    with pytest.raises(ValidationError):
        edit(options=["a", "b", "c", " "])
    with pytest.raises(ValidationError):
        edit(options=["a", "b", "c"])


def test_paper_item_has_no_answer_field():
    assert "answer" not in PaperItem.model_fields
    assert "why" not in PaperItem.model_fields
    assert not issubclass(PaperItem, KeyItem)
    assert "answer" in KeyItem.model_fields

"""scripts/seed_coding_questions.py: the seed data is valid and the guard holds. No database."""

import pytest

from scripts.seed_coding_questions import (
    balanced,
    check_target,
    count_even,
    max_subarray,
    question_payloads,
)


def test_three_valid_questions_of_easy_and_medium():
    qs = question_payloads()  # validated like a POST /questions body
    assert [q.difficulty for q in qs] == ["easy", "medium", "medium"]
    for q in qs:
        assert [c.is_sample for c in q.test_cases] == [True] * 3 + [False] * 15
        assert len({c.input for c in q.test_cases}) == 18  # no duplicate cases
        assert "seed" in q.tags and q.body.splitlines()[0]  # first line is the title


def test_seed_data_is_the_same_every_run():
    assert question_payloads() == question_payloads()


def test_hidden_cases_are_not_all_one_answer():
    for q in question_payloads():
        assert len({c.expected for c in q.test_cases if not c.is_sample}) > 1


@pytest.mark.parametrize(
    "fn,arg,out",
    [
        (count_even, [1, 2, 3, 4], "2\n"),
        (count_even, [-2, 0, 7], "2\n"),
        (balanced, "()[]{}", "YES\n"),
        (balanced, "([)]", "NO\n"),
        (balanced, "(", "NO\n"),
        (balanced, "]", "NO\n"),
        (max_subarray, [-2, 1, -3, 4, -1, 2, 1, -5, 4], "6\n"),
        (max_subarray, [-3, -1, -2], "-1\n"),
    ],
)
def test_reference_solutions(fn, arg, out):
    assert fn(arg) == out


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_local_databases_are_allowed(host):
    assert check_target(f"postgresql+asyncpg://u:p@{host}:5432/db", False) == host


def test_a_remote_database_needs_allow_remote():
    url = "postgresql+asyncpg://u:p@db.example.supabase.co:5432/postgres"
    with pytest.raises(SystemExit, match="refusing"):
        check_target(url, False)
    assert check_target(url, True) == "db.example.supabase.co"

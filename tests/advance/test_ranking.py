"""Ranking math for the cut-off (app/advance/ranking.py). No database."""

import uuid
from decimal import Decimal

from app.advance import ranking


def row(name, status="submitted", marks=None, max_marks=None, secs=None):
    return dict(
        candidate_id=uuid.uuid4(),
        student_id=f"S-{name}",
        name=name,
        attempt_status=status,
        marks=None if marks is None else Decimal(str(marks)),
        max_marks=None if max_marks is None else Decimal(str(max_marks)),
        time_taken_sec=secs,
    )


def by_name(entries):
    return {e.name: e for e in entries}


def test_percent_uses_each_students_own_max_marks():
    # 30/60 = 50 %, 20/25 = 80 %, 40/100 = 40 %: raw marks would put Cara first
    entries, counts = ranking.rank(
        [row("Asha", marks=30, max_marks=60), row("Ben", marks=20, max_marks=25),
         row("Cara", marks=40, max_marks=100)],
        50,
    )  # fmt: skip
    assert [(e.name, e.percent, e.rank) for e in entries] == [
        ("Ben", 80.0, 1),
        ("Asha", 50.0, 2),
        ("Cara", 40.0, 3),
    ]
    assert [e.would_pass for e in entries] == [True, True, False]
    assert counts.model_dump(by_alias=True) == {
        "pass": 2, "fail": 1, "absent": 0, "still_writing": 0, "locked": 0,
    }  # fmt: skip


def test_equal_percent_ranks_the_faster_student_first():
    entries, _ = ranking.rank(
        [row("Slow", marks=5, max_marks=10, secs=900),
         row("Fast", marks=10, max_marks=20, secs=300),
         row("NoTime", marks=1, max_marks=2, secs=None)],
        None,
    )  # fmt: skip
    assert [(e.name, e.rank) for e in entries] == [("Fast", 1), ("Slow", 2), ("NoTime", 3)]


def test_full_tie_is_still_a_stable_order():
    rows = [row("B", marks=5, max_marks=10, secs=60), row("A", marks=5, max_marks=10, secs=60)]
    first, _ = ranking.rank(rows, None)
    second, _ = ranking.rank(list(reversed(rows)), None)
    assert [e.student_id for e in first] == [e.student_id for e in second] == ["S-A", "S-B"]
    assert [e.rank for e in first] == [1, 2]


def test_threshold_equal_to_percent_passes():
    entries, _ = ranking.rank(
        [row("Half", marks=5, max_marks=10), row("Just", marks=4.999, max_marks=10)], 50
    )
    assert by_name(entries)["Half"].would_pass is True
    assert (
        by_name(entries)["Just"].percent == 49.99 and by_name(entries)["Just"].would_pass is False
    )


def test_cut_off_compares_the_rounded_percent_hr_sees():
    entries, _ = ranking.rank([row("Two", marks=2, max_marks=3)], 66.67)  # 66.666... -> 66.67
    assert entries[0].percent == 66.67 and entries[0].would_pass is True
    entries, _ = ranking.rank([row("Two", marks=2, max_marks=3)], 66.68)
    assert entries[0].would_pass is False


def test_absent_never_started_and_no_attempt_are_not_ranked():
    entries, counts = ranking.rank(
        [row("Done", marks=7, max_marks=10), row("Gone", status=None),
         row("Idle", status="not_started", max_marks=10)],
        60,
    )  # fmt: skip
    gone, idle = by_name(entries)["Gone"], by_name(entries)["Idle"]
    assert (gone.rank, gone.percent, gone.would_pass) == (None, None, False)
    assert (idle.rank, idle.percent, idle.would_pass) == (None, None, False)
    assert counts.absent == 2 and counts.pass_ == 1 and counts.fail == 0


def test_still_writing_and_locked_are_counted_but_undecided():
    entries, counts = ranking.rank(
        [row("Lock", status="locked"), row("Busy", status="in_progress"),
         row("Gone", status=None), row("Done", marks=1, max_marks=10)],
        50,
    )  # fmt: skip
    # finished first (by rank), then still writing, locked, absent
    assert [e.name for e in entries] == ["Done", "Busy", "Lock", "Gone"]
    assert by_name(entries)["Busy"].would_pass is None
    assert by_name(entries)["Lock"].would_pass is None
    assert (counts.still_writing, counts.locked, counts.absent) == (1, 1, 1)
    assert (counts.pass_, counts.fail) == (0, 1)


def test_no_threshold_means_no_verdicts():
    entries, counts = ranking.rank(
        [row("A", marks=9, max_marks=10), row("Gone", status=None)], None
    )
    assert [e.would_pass for e in entries] == [None, None]
    assert counts.pass_ is None and counts.fail is None and counts.absent == 1
    assert entries[0].rank == 1 and entries[0].percent == 90.0


def test_expired_counts_as_finished_and_missing_marks_are_zero():
    entries, _ = ranking.rank(
        [row("Late", status="expired", marks=6, max_marks=10),
         row("Blank", marks=None, max_marks=10)],
        50,
    )  # fmt: skip
    assert by_name(entries)["Late"].rank == 1 and by_name(entries)["Late"].would_pass is True
    assert by_name(entries)["Blank"].percent == 0.0 and by_name(entries)["Blank"].marks == 0.0


def test_paper_with_no_marks_scores_zero_percent():
    entries, _ = ranking.rank([row("Empty", marks=0, max_marks=0)], 0)
    assert entries[0].percent == 0.0 and entries[0].would_pass is True  # 0 >= 0
    entries, _ = ranking.rank([row("Empty", marks=0, max_marks=0)], 1)
    assert entries[0].would_pass is False

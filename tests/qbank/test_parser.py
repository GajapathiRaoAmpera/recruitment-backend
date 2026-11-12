"""PDF question-bank parser on plain text pages (layout copied from core_cs_500_mcq.pdf)."""

import os
import time
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.qbank import parser
from app.qbank.parser import SUBJECTS, extract_pages, parse_pages

PROG, DSA, OS_ = SUBJECTS[0], SUBJECTS[1], SUBJECTS[2]


def block(n, level, topic, stem, opts=("w", "x", "y", "z")) -> str:
    lines = [f"{n}. {level} - {topic}", stem]
    lines += [f"{chr(65 + i)}. {o}" for i, o in enumerate(opts)]
    return "\n".join(lines)


def key(subject, rows) -> str:
    return "\n".join([subject, "# Level Answer", *(f"{n} {lvl} {a}" for n, lvl, a in rows)])


LIB = "\n".join(
    [
        "1. Easy - Libraries",
        "What is the difference between a library and a framework?",
        "A. A framework is only used for testing",
        "B. Your code calls a library; a framework calls your code",
        "C. A library is always larger",
        "D. There is no difference",
    ]
)


def test_parses_basic_block():
    bank = parse_pages([PROG + "\n" + LIB, "Answer Key\n" + key(PROG, [(1, "Easy", "B")])])
    assert bank.report.skipped == [] and bank.report.warnings == []
    (q,) = bank.questions
    assert q.subject == PROG and q.number == 1 and q.level == "easy" and q.topic == "Libraries"
    assert q.text == "What is the difference between a library and a framework?"
    assert q.options[1] == "Your code calls a library; a framework calls your code"
    assert len(q.options) == 4 and q.answer == 1 and q.warnings == ()


def test_wrapped_stem_and_option():
    page = "\n".join(
        [
            PROG,
            "1. Medium - Inheritance",
            "Which statement about multiple inheritance in a language",
            "that supports it is correct?",
            "A. It is never allowed",
            "B. A class may inherit from more than one base class, which can",
            "lead to the diamond problem",
            "C. It removes the need for interfaces",
            "D. It only applies to Object-",
            "Oriented code",
        ]
    )
    bank = parse_pages([page, "Answer Key\n" + key(PROG, [(1, "Medium", "B")])])
    (q,) = bank.questions
    assert (
        q.text
        == "Which statement about multiple inheritance in a language that supports it is correct?"
    )
    assert q.options[1] == (
        "A class may inherit from more than one base class, which can lead to the diamond problem"
    )
    assert q.options[3] == "It only applies to Object-Oriented code"
    assert bank.report.warnings == []


def test_subject_heading_after_last_question():
    page = "\n".join(
        [
            block(84, "Hard", "Patterns", "Last prog question?"),
            DSA,
            block(1, "Easy", "Arrays", "First DSA question?"),
            "Page 7",
        ]
    )
    keys = "Answer Key\n" + key(PROG, [(84, "Hard", "A")]) + "\n" + key(DSA, [(1, "Easy", "C")])
    bank = parse_pages([PROG, page, keys])
    assert [(q.subject, q.number) for q in bank.questions] == [(PROG, 84), (DSA, 1)]
    assert bank.questions[0].options[3] == "z"  # heading not glued onto option D
    assert bank.report.warnings == []


def test_unknown_heading_after_last_question_is_accepted():
    page = "\n".join(
        [
            PROG,
            block(1, "Easy", "T", "Q?"),
            "Cloud Computing Basics",
            block(1, "Easy", "T", "Cloud Q?"),
        ]
    )
    keys = (
        "Answer Key\n"
        + key(PROG, [(1, "Easy", "A")])
        + "\n"
        + key("Cloud Computing Basics", [(1, "Easy", "D")])
    )
    bank = parse_pages([page, keys])
    assert [(q.subject, q.answer) for q in bank.questions] == [
        (PROG, 0),
        ("Cloud Computing Basics", 3),
    ]
    assert bank.questions[0].options[3] == "z"
    assert parser.subject_slug("Cloud Computing Basics") == "cloud_computing_basics"


def test_levels_not_monotonic():
    page = "\n".join(
        [DSA]
        + [block(n, "Hard", "Graphs", f"Q{n}?") for n in (46, 47)]
        + [block(n, "Medium", "Heaps", f"Q{n}?") for n in (74, 75)]
        + [block(81, "Easy", "Lists", "Q81?")]
    )
    rows = [
        (46, "Hard", "A"),
        (47, "Hard", "B"),
        (74, "Medium", "C"),
        (75, "Medium", "D"),
        (81, "Easy", "A"),
    ]
    bank = parse_pages([page, "Answer Key\n" + key(DSA, rows)])
    assert [q.level for q in bank.questions] == ["advanced", "advanced", "medium", "medium", "easy"]
    assert [q.answer for q in bank.questions] == [0, 1, 2, 3, 0]
    assert bank.report.warnings == []


def _two(level_a="Easy", level_b="Medium"):
    return "\n".join([OS_, block(1, level_a, "T", "Q1?"), block(2, level_b, "T", "Q2?")])


def test_key_spaced():
    bank = parse_pages(
        [_two(), "Answer Key\nOperating Systems\n# Level Answer\n1 Easy B\n2 Medium D"]
    )
    assert [q.answer for q in bank.questions] == [1, 3]
    assert bank.report.warnings == []


def test_key_glued_level_answer():
    bank = parse_pages(
        [_two(), "Answer Key\nOperating Systems\n# Level Answer\n1 EasyB\n2 MediumB"]
    )
    assert [q.answer for q in bank.questions] == [1, 1]
    assert bank.report.warnings == []


def test_key_run_on_single_line():
    qs = "\n".join([OS_] + [block(n, "Easy", "T", f"Q{n}?") for n in range(1, 12)])
    keys = "\n".join(
        [
            "Answer Key",
            "Operating Systems # Level Answer 1 Easy A 2 Easy C 3 Easy B 4 Easy D 5 Easy A "
            "6 Easy B "
            "7 Easy C 8 Easy D 9 Easy B",
            "10 Easy A",
            "11 EasyC",
        ]
    )
    bank = parse_pages([qs, keys])
    assert [q.answer for q in bank.questions] == [0, 2, 1, 3, 0, 1, 2, 3, 1, 0, 2]
    assert bank.report.warnings == [] and bank.report.skipped == []


def test_key_table_split_across_pages_and_next_subject_after_last_row():
    qs = "\n".join(
        [
            PROG,
            block(1, "Easy", "T", "P1?"),
            block(2, "Easy", "T", "P2?"),
            DSA,
            block(1, "Hard", "T", "D1?"),
        ]
    )
    keys = [
        "Answer Key\nProgramming and Object-Oriented Design\n# Level Answer\n1 Easy A\nPage 30",
        "2 Easy B\nData Structures and Algorithms # Level Answer 1 HardC\nPage 31",
    ]
    bank = parse_pages([qs, *keys])
    assert [(q.subject, q.number, q.answer) for q in bank.questions] == [
        (PROG, 1, 0),
        (PROG, 2, 1),
        (DSA, 1, 2),
    ]
    assert bank.report.warnings == []


def test_level_mismatch_warns():
    bank = parse_pages(
        [_two(), "Answer Key\nOperating Systems\n# Level Answer\n1 Hard B\n2 Medium D"]
    )
    assert len(bank.questions) == 2  # kept
    q = bank.questions[0]
    assert q.level == "easy"  # the block's own level wins
    assert q.warnings and "level" in q.warnings[0].lower()
    assert any("level" in w.lower() for w in bank.report.warnings)


def test_missing_key_entry_skips_question():
    bank = parse_pages(
        [_two(), "Answer Key\nOperating Systems\n# Level Answer\n1 Easy B\n3 Easy A"]
    )
    assert [q.number for q in bank.questions] == [1]
    assert len(bank.report.skipped) == 1 and "2" in bank.report.skipped[0]
    assert any("no answer" in w.lower() for w in bank.report.warnings)
    assert any("no question" in w.lower() for w in bank.report.warnings)  # key row 3


def test_five_options_skipped():
    page = "\n".join(
        [
            OS_,
            block(1, "Easy", "T", "Q1?", ("a", "b", "c", "d", "e")),
            block(2, "Medium", "T", "Q2?"),
        ]
    )
    bank = parse_pages(
        [page, "Answer Key\nOperating Systems\n# Level Answer\n1 Easy B\n2 Medium D"]
    )
    assert [q.number for q in bank.questions] == [2]
    assert len(bank.report.skipped) == 1 and "5 options" in bank.report.skipped[0]


def test_three_options_skipped():
    page = "\n".join([OS_, block(1, "Easy", "T", "Q1?", ("a", "b", "c"))])
    bank = parse_pages([page, "Answer Key\nOperating Systems\n# Level Answer\n1 Easy B"])
    assert bank.questions == [] and "3 options" in bank.report.skipped[0]


def test_hard_maps_to_advanced():
    bank = parse_pages(
        [_two("Hard", "Hard"), "Answer Key\n" + key(OS_, [(1, "Hard", "A"), (2, "Hard", "B")])]
    )
    assert {q.level for q in bank.questions} == {"advanced"}
    assert bank.report.by_level == {"easy": 0, "medium": 0, "advanced": 2}


def test_options_with_symbols():
    opts = ("7 // 2", "x & (x - 1)", "O(n^2) and O(n*m)", "\"a\" < 'b' / 2")
    page = "\n".join(
        [DSA, block(1, "Medium", "Bits", "What does x & (x - 1) do, and is 7 // 2 == 3?", opts)]
    )
    bank = parse_pages([page, "Answer Key\n" + key(DSA, [(1, "Medium", "B")])])
    (q,) = bank.questions
    assert q.options == opts
    assert q.text == "What does x & (x - 1) do, and is 7 // 2 == 3?"


def test_numbering_restarts_per_subject():
    page = "\n".join([PROG, block(1, "Easy", "T", "P1?"), DSA, block(1, "Easy", "T", "D1?")])
    keys = "Answer Key\n" + key(PROG, [(1, "Easy", "A")]) + "\n" + key(DSA, [(1, "Easy", "D")])
    bank = parse_pages([page, keys])
    assert [(q.subject, q.number, q.answer) for q in bank.questions] == [(PROG, 1, 0), (DSA, 1, 3)]


def test_summary_page_and_footers_ignored():
    summary = "\n".join(
        [
            "Core CS 500 MCQ Bank",
            "Subject Questions Easy Medium Hard",
            "Programming and Object-Oriented Design 84 20 34 30",
            "Data Structures and Algorithms 84 22 32 30",
            "Total 500 134 198 168",
            "Page 1",
        ]
    )
    page2 = "\n".join([PROG, block(1, "Easy", "T", "Q?"), "Page 2"])
    bank = parse_pages(
        [summary, page2, "Answer Key\n" + key(PROG, [(1, "Easy", "C")]) + "\nPage 3"]
    )
    (q,) = bank.questions
    assert q.options[3] == "z" and q.answer == 2
    assert bank.report.warnings == []


def test_question_spanning_page_break():
    p2 = "\n".join([PROG, "1. Easy - T", "A stem", "A. one", "B. two", "Page 2"])
    p3 = "\n".join(["C. three", "D. four", "Page 3"])
    bank = parse_pages([p2, p3, "Answer Key\n" + key(PROG, [(1, "Easy", "D")])])
    (q,) = bank.questions
    assert q.options == ("one", "two", "three", "four") and q.answer == 3


def test_empty_input():
    bank = parse_pages([])
    assert bank.questions == [] and bank.report.total == 0 and bank.report.warnings
    blank = parse_pages(["", "   "])
    assert blank.questions == [] and blank.report.warnings


def test_garbage_never_raises():
    bank = parse_pages(
        ["%%%\n1. Easy -\nA.\nB.\n\x00", "Answer Key\n# Level Answer\n99 Hard Z\nfoo"]
    )
    assert bank.questions == []
    assert bank.report.warnings or bank.report.skipped


def test_no_answer_key_skips_all():
    bank = parse_pages([PROG + "\n" + LIB])
    assert bank.questions == [] and len(bank.report.skipped) == 1
    assert any("answer key" in w.lower() for w in bank.report.warnings)


def test_report_counts():
    page = "\n".join(
        [
            PROG,
            block(1, "Easy", "T", "a"),
            block(2, "Hard", "T", "b"),
            DSA,
            block(1, "Medium", "T", "c"),
        ]
    )
    keys = (
        "Answer Key\n"
        + key(PROG, [(1, "Easy", "A"), (2, "Hard", "B")])
        + "\n"
        + key(DSA, [(1, "Medium", "C")])
    )
    report = parse_pages([page, keys]).report
    assert report.total == 3
    assert report.by_subject == {PROG: 2, DSA: 1}
    assert report.by_level == {"easy": 1, "medium": 1, "advanced": 1}
    assert report.skipped == [] and report.warnings == []


def _minimal_pdf(pages: list[list[str]]) -> bytes:
    """Hand-built PDF (Helvetica, one Tj per line): tests extract_pages with no new dependency."""
    n = len(pages)
    font_id = 3 + 2 * n
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{3 + 2 * i} 0 R".encode() for i in range(n))
        + f"] /Count {n} >>".encode(),
    ]
    for i, lines in enumerate(pages):
        ops = ["BT /F1 12 Tf 14 TL 72 720 Td"]
        for line in lines:
            esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({esc}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_extract_pages_reads_text_per_page():
    data = _minimal_pdf(
        [
            [
                PROG,
                "1. Easy - Bits",
                "What is 7 // 2 in Python?",
                "A. 3",
                "B. 3.5",
                "C. 4",
                "D. x & (x - 1)",
                "Page 2",
            ],
            ["Answer Key", PROG, "# Level Answer", "1 Easy A"],
        ]
    )
    pages = extract_pages(data)
    assert len(pages) == 2
    assert "1. Easy - Bits" in pages[0] and "Page 2" in pages[0]
    bank = parse_pages(pages)
    (q,) = bank.questions
    assert q.options == ("3", "3.5", "4", "x & (x - 1)") and q.answer == 0


@pytest.mark.parametrize("data", [b"", b"not a pdf at all", b"%PDF-1.4\n garbage \x00\xff"])
def test_extract_pages_unreadable(data):
    with pytest.raises(AppError) as exc:
        extract_pages(data)
    assert exc.value.status == 422 and exc.value.code == "PDF_UNREADABLE"


def test_extract_pages_too_many_pages():
    data = _minimal_pdf([["page one"], ["page two"]])
    with pytest.raises(AppError) as exc:
        extract_pages(data, max_pages=1)
    assert exc.value.status == 422 and exc.value.code == "PDF_TOO_MANY_PAGES"
    assert len(extract_pages(data, max_pages=2)) == 2


@pytest.mark.parametrize("dash", ["\u2013", "\u2014", ":", "-"])
def test_question_header_dash_variants(dash):
    page = "\n".join([OS_, f"1. Easy {dash} Paging", "Q?", "A. a", "B. b", "C. c", "D. d"])
    bank = parse_pages([page, "Answer Key\n" + key(OS_, [(1, "Easy", "B")])])
    (q,) = bank.questions
    assert q.topic == "Paging" and q.text == "Q?" and q.answer == 1
    assert bank.report.warnings == []


def test_question_header_swallowed_into_option_warns():
    page = "\n".join(
        [
            OS_,
            block(1, "Easy", "T", "Q1?", ("a", "b", "c", "d 2. Medium \u2013 Scheduling")),
            "Q2?",
        ]
    )
    bank = parse_pages([page, "Answer Key\n" + key(OS_, [(1, "Easy", "A"), (2, "Medium", "B")])])
    assert any("question header" in w for w in bank.report.warnings)
    (q,) = bank.questions  # Q1 kept (with the note); Q2 has no block, its key row is an orphan
    assert any("question header" in w for w in q.warnings)


def test_duplicate_question_number_in_subject_skipped():
    page = "\n".join([OS_, block(2, "Easy", "T", "first?"), block(2, "Easy", "T", "second?")])
    bank = parse_pages([page, "Answer Key\n" + key(OS_, [(2, "Easy", "A")])])
    assert [q.text for q in bank.questions] == ["first?"]
    assert len(bank.report.skipped) == 1 and "duplicate question number" in bank.report.skipped[0]


def test_empty_stem_skipped():
    page = "\n".join([OS_, "1. Easy - T", "A. a", "B. b", "C. c", "D. d"])
    bank = parse_pages([page, "Answer Key\n" + key(OS_, [(1, "Easy", "A")])])
    assert bank.questions == [] and "empty question text" in bank.report.skipped[0]


def test_empty_option_skipped():
    page = "\n".join([OS_, "1. Easy - T", "Q?", "A. a", "B.", "C. c", "D. d"])
    bank = parse_pages([page, "Answer Key\n" + key(OS_, [(1, "Easy", "A")])])
    assert bank.questions == [] and "empty option B" in bank.report.skipped[0]


def test_duplicate_key_row_first_kept():
    keys = "Answer Key\n" + key(OS_, [(1, "Easy", "B"), (1, "Easy", "D"), (2, "Medium", "A")])
    bank = parse_pages([_two(), keys])
    assert [q.answer for q in bank.questions] == [1, 0]
    assert any("duplicate row" in w for w in bank.report.warnings)


def test_out_of_order_option_letter_is_a_wrap():
    page = "\n".join(
        [OS_, "1. Easy - T", "Q?", "A. a", "B. b", "C. see", "B. tree traversal", "D. d"]
    )
    bank = parse_pages([page, "Answer Key\n" + key(OS_, [(1, "Easy", "C")])])
    (q,) = bank.questions
    assert q.options == ("a", "b", "see B. tree traversal", "d")


def test_inline_page_footer_in_key_is_stripped():
    keys = "Answer Key\nOperating Systems\n# Level Answer\n1 Easy B Page 30 2 Medium D"
    bank = parse_pages([_two(), keys])
    assert [q.answer for q in bank.questions] == [1, 3]
    assert bank.report.warnings == []


def test_many_orphan_key_rows_stay_linear():
    """Hostile input: thousands of unknown-subject blocks and orphan key rows (was quadratic)."""
    q_lines, key_parts = [], []
    for s in range(12):
        name = f"Subject {chr(65 + s)}{chr(97 + s)}x"
        q_lines.append(name)
        q_lines += [block(n, "Easy", "T", f"Q{n}?") for n in range(1, 501)]
        key_parts.append(
            f"{name} # Level Answer " + " ".join(f"{n} Easy A" for n in range(501, 1000))
        )
    started = time.perf_counter()
    bank = parse_pages(["\n".join(q_lines), "Answer Key\n" + "\n".join(key_parts)])
    assert time.perf_counter() - started < 5
    assert len(bank.report.skipped) == 12 * 500
    assert sum("no question with this number" in w for w in bank.report.warnings) == 12 * 499


def test_real_pdf_if_present():
    path = os.environ.get("QBANK_SAMPLE_PDF")
    if not path or not Path(path).is_file():
        pytest.skip("set QBANK_SAMPLE_PDF to core_cs_500_mcq.pdf to run")
    bank = parse_pages(extract_pages(Path(path).read_bytes()))
    report = bank.report
    assert report.skipped == [], report.skipped[:10]
    assert report.total == 500
    assert len(report.by_subject) == 6 and set(report.by_subject) == set(SUBJECTS)
    assert report.by_level == {"easy": 134, "medium": 198, "advanced": 168}
    assert all(len(q.options) == 4 and 0 <= q.answer < 4 for q in bank.questions)


def test_known_heading_glued_to_last_option_is_recovered():
    page = "\n".join(
        [
            PROG,
            block(1, "Easy", "T", "P1?", ("a", "b", "c", "There is no difference " + DSA)),
            block(1, "Easy", "T", "D1?"),
        ]
    )
    keys = "Answer Key\n" + key(PROG, [(1, "Easy", "D")]) + "\n" + key(DSA, [(1, "Easy", "A")])
    bank = parse_pages([page, keys])
    assert [(q.subject, q.number) for q in bank.questions] == [(PROG, 1), (DSA, 1)]
    assert bank.questions[0].options[3] == "There is no difference"


def test_synthetic_full_bank_mixed_key_variants():
    counts = dict(zip(SUBJECTS, (84, 84, 83, 83, 83, 83), strict=True))
    levels = ["Easy", "Hard", "Medium"]
    q_pages, key_lines, page_no = ["Summary\nTotal 500\nPage 1"], ["Answer Key"], 2
    for s_idx, (subject, n) in enumerate(counts.items()):
        lines = [subject]
        rows = []
        for num in range(1, n + 1):
            lvl = levels[(num * 7 + s_idx) % 3]
            lines.append(
                block(
                    num,
                    lvl,
                    "Topic",
                    f"Stem {num} with x & (x - 1)?",
                    ("7 // 2", "O(n^2)", "a < b", '"q"'),
                )
            )
            rows.append(f"{num} {lvl}{'' if num % 5 == 0 else ' '}{'ABCD'[num % 4]}")
            if num % 12 == 0:
                lines.append(f"Page {page_no}")
                page_no += 1
        q_pages.append("\n".join(lines))
        key_lines.append(f"{subject} # Level Answer " + " ".join(rows[:9]))
        key_lines += rows[9:]
    bank = parse_pages([*q_pages, "\n".join(key_lines)])
    assert bank.report.skipped == [] and bank.report.warnings == []
    assert bank.report.total == 500 and bank.report.by_subject == counts
    assert all(q.answer == q.number % 4 for q in bank.questions)

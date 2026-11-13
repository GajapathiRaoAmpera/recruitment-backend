"""AC-2 building blocks: CGPA, skills and JD match on plain text (no PDFs needed)."""

import pytest

from app.candidates import parser


@pytest.mark.parametrize(
    "text,expected",
    [
        ("B.Tech CSE, CGPA: 8.52", 8.52),
        ("CGPA - 9", 9.0),
        ("8.7/10 CGPA", 8.7),
        ("GPA 3.6 / 4", 9.0),
        ("Score 8.1/10", 8.1),
        ("CPI : 7.45 (till 6th sem)", 7.45),
        ("Percentage: 85%", None),
        ("CGPA: 12.5", None),  # impossible value
        ("no grades here", None),
    ],
)
def test_cgpa(text, expected):
    assert parser.extract_cgpa(text) == expected


def test_skills_respect_word_boundaries():
    text = "Skills: Java, JavaScript, C++, SQL (PostgreSQL), Node.js, Python3 basics"
    got = parser.extract_skills(text)
    assert {"java", "javascript", "c++", "sql", "node.js"} <= set(got)
    assert "c" not in got  # "C++" must not count as C
    assert "python" not in got  # "Python3" is a different token: shortcut, fine for now


def test_java_does_not_match_javascript_alone():
    assert parser.extract_skills("I know JavaScript") == ["javascript"]


def test_c_is_case_sensitive():
    assert "c" in parser.extract_skills("Languages: C, Java")
    assert "c" not in parser.extract_skills("a c b")


def test_jd_match():
    assert parser.jd_match(["java", "sql"], "Need Java, SQL, Docker and Git") == 50.0
    assert parser.jd_match(["java"], "Be a team player") is None


def test_analyse_bad_pdf_is_failed_not_raised():
    r = parser.analyse(b"not a pdf", "Java")
    assert r["parse_status"] == "failed" and r["skills"] == [] and r["cgpa"] is None

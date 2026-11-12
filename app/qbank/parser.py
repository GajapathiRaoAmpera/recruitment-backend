"""MCQ question-bank PDF parser.

`parse_pages` is pure (page text in, `ParsedBank` out) and never raises on odd content: problems
land in `report.warnings`, dropped questions in `report.skipped`. `extract_pages` is the only I/O.

Layout (core_cs_500_mcq.pdf): page 1 summary table, `Page N` footers, one heading line per subject,
blocks `N. Level - Topic` / stem (may wrap) / `A.`..`D.` options (may wrap), numbering restarting
per subject, then `Answer Key` with one `# Level Answer` table per subject. Questions are joined to
key rows by (subject, number).
"""

import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import pdfplumber

from app.core.errors import AppError
from app.papers.schemas import LEVELS, Level

SUBJECTS: tuple[str, ...] = (
    "Programming and Object-Oriented Design",
    "Data Structures and Algorithms",
    "Operating Systems",
    "Database Management Systems",
    "Computer Networks",
    "Software Engineering",
)

_LEVEL_MAP: dict[str, Level] = {"easy": "easy", "medium": "medium", "hard": "advanced"}
_LEVEL_WORD = {"easy": "Easy", "medium": "Medium", "advanced": "Hard"}
OPTION_COUNT = 4

_FOOTER = re.compile(r"^page\s+\d+(?:\s+of\s+\d+)?$", re.I)
# hyphen-minus, en dash, em dash, colon (escaped so an encoding round-trip cannot mangle them)
_DASH = r"[\-\u2013\u2014:]"
_QSTART = re.compile(rf"^(\d{{1,3}})\s*[.)]\s*(easy|medium|hard)\b\s*(?:{_DASH}\s*(.*))?$", re.I)
# a question header swallowed into an option line (lost line break): warn, never silently merge
_EMBEDDED_QSTART = re.compile(rf"(?<!\d)\d{{1,3}}\s*[.)]\s*(?:easy|medium|hard)\s*{_DASH}", re.I)
_OPTION = re.compile(r"^([A-H])[.)](?:\s+(.*))?$")
_ANSWER_KEY = re.compile(r"^answer\s*key\b\s*(.*)$", re.I)
_KEY_TOKEN = re.compile(
    r"(?P<hdr>#\s*(?i:level)\s*(?i:answer))"
    r"|(?<!\d)(?P<num>\d{1,3})\s*(?P<lvl>(?i:easy|medium|hard))\s*(?P<ans>[A-D])(?![A-Za-z])"
)
_KEY_FOOTER = re.compile(r"\bpage\s+\d+(?:\s+of\s+\d+)?\b", re.I)
_HEADING_LIKE = re.compile(r"[A-Za-z][A-Za-z&/,'()\- ]{2,79}")


def subject_slug(name: str) -> str:
    """Stable key for a subject heading: 'Object-Oriented Design' -> 'object_oriented_design'."""
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


_KNOWN = {subject_slug(s): s for s in SUBJECTS}


@dataclass(frozen=True)
class ParsedQuestion:
    subject: str
    number: int
    level: Level
    topic: str
    text: str
    options: tuple[str, ...]
    answer: int  # 0-based index into options
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParseReport:
    total: int
    by_subject: dict[str, int]
    by_level: dict[str, int]
    warnings: list[str]
    skipped: list[str]


@dataclass(frozen=True)
class ParsedBank:
    questions: list[ParsedQuestion]
    report: ParseReport


@dataclass
class _Block:
    subject: str
    number: int
    level: Level
    topic: str
    stem: list[str] = field(default_factory=list)
    options: list[list[str]] = field(default_factory=list)  # each option's wrapped lines
    notes: list[str] = field(default_factory=list)  # warnings that keep the question

    @property
    def label(self) -> str:
        return f"{self.subject} #{self.number}"


def _clean(line: str) -> str:
    return " ".join(line.replace("\x00", " ").split())


def _join(parts: list[str]) -> str:
    out = ""
    for part in parts:
        if not out:
            out = part
        elif re.search(r"[A-Za-z]-$", out):  # "Object-" + "Oriented" -> "Object-Oriented"
            out += part
        else:
            out += " " + part
    return out.strip()


def _known(text: str) -> str | None:
    return _KNOWN.get(subject_slug(text))


def _trailing_known(text: str, exclude: str | None) -> str | None:
    """A known heading glued to the end of a line (extraction lost the line break)."""
    slug = subject_slug(text)
    for key, name in _KNOWN.items():
        if name != exclude and slug.endswith(key) and slug != key:
            return name
    return None


def _lines(pages: Sequence[str]) -> list[str]:
    out = []
    for page in pages:
        for raw in (page or "").splitlines():
            line = _clean(raw)
            if line and not _FOOTER.match(line):
                out.append(line)
    return out


def _split_questions(
    lines: list[str], warnings: list[str]
) -> tuple[list[_Block], list[str] | None]:
    """Line state machine over the question section: blocks plus the answer-key lines (or None)."""
    blocks: list[_Block] = []
    block: _Block | None = None
    subject: str | None = None
    heading_since_q = False
    preamble_last: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]

        m = _ANSWER_KEY.match(line)
        if m and (not m.group(1) or _key_rest_ok(m.group(1))):
            if block:
                blocks.append(block)
            rest = [m.group(1)] if m.group(1) else []
            return blocks, rest + lines[i + 1 :]

        known = _known(line)
        if not known and i + 1 < len(lines):
            two = _known(f"{line} {lines[i + 1]}")
            if two:
                known, i = two, i + 1
        if known:
            if block:
                blocks.append(block)
                block = None
            subject, heading_since_q = known, True
            i += 1
            continue

        q = _QSTART.match(line)
        if q:
            number, level, topic = (
                int(q.group(1)),
                _LEVEL_MAP[q.group(2).lower()],
                (q.group(3) or "").strip(),
            )
            if block and not heading_since_q and number <= block.number:
                new_subject = _recover_heading(block) if number == 1 else None
                if new_subject:
                    subject = new_subject
                else:
                    warnings.append(
                        f"{block.subject}: question numbering went back to {number} after "
                        f"{block.number} without a subject heading"
                    )
            if block:
                blocks.append(block)
            if subject is None:
                if preamble_last and _HEADING_LIKE.fullmatch(preamble_last):
                    subject = preamble_last
                else:
                    subject = "Unknown subject"
                    warnings.append(
                        "Questions found before any subject heading; filed under 'Unknown subject'"
                    )
            block = _Block(subject, number, level, topic)
            heading_since_q = False
            i += 1
            continue

        if block is None:
            preamble_last = line
            i += 1
            continue

        o = _OPTION.match(line)
        if o and o.group(1) == chr(ord("A") + len(block.options)):
            block.options.append([o.group(2) or ""])
        elif block.options:
            block.options[-1].append(line)
        else:
            block.stem.append(line)
        if block.options and _EMBEDDED_QSTART.search(line):
            note = (
                f"{block.label}: option text contains a question header ({line[:80]!r}); "
                "check the PDF"
            )
            block.notes.append(note)
            warnings.append(note)
        i += 1

    if block:
        blocks.append(block)
    return blocks, None


def _key_rest_ok(rest: str) -> bool:
    return rest.startswith("#") or any(subject_slug(rest).startswith(k) for k in _KNOWN)


def _recover_heading(block: _Block) -> str | None:
    """Numbering restarted at 1 with no heading line seen: the heading was swallowed as a wrap
    of the previous question's last option (unknown subject) or glued onto its end (known)."""
    if not block.options:
        return None
    last = block.options[-1]
    if len(last) > 1 and _HEADING_LIKE.fullmatch(last[-1]):
        return last.pop()
    glued = _trailing_known(last[-1], exclude=block.subject)
    if glued:
        words = len(glued.split())
        last[-1] = " ".join(last[-1].split()[:-words])
        return glued
    return None


def _parse_key(lines: list[str], warnings: list[str]) -> dict[tuple[str, int], tuple[Level, int]]:
    """All three extraction variants become one token stream: spaced rows, glued `36 MediumB`, and
    run-on lines like `Operating Systems # Level Answer 1 Easy A 2 Easy C`."""
    text = _KEY_FOOTER.sub(" ", " ".join(lines))
    keys: dict[tuple[str, int], tuple[Level, int]] = {}
    subject: str | None = None
    tokens = list(_KEY_TOKEN.finditer(text))
    pos = 0
    for tok in [*tokens, None]:
        residual = _clean(text[pos : tok.start() if tok else len(text)])
        if residual:
            name = _key_subject(
                residual, next_is_header=bool(tok and tok.group("hdr")), warnings=warnings
            )
            if name:
                subject = name
        if tok is None:
            break
        pos = tok.end()
        if tok.group("hdr"):
            continue
        number = int(tok.group("num"))
        if subject is None:
            warnings.append(f"Answer key row {number} appears before any subject heading; ignored")
            continue
        k = (subject_slug(subject), number)
        if k in keys:
            warnings.append(f"Answer key {subject} #{number}: duplicate row; first one kept")
            continue
        keys[k] = (_LEVEL_MAP[tok.group("lvl").lower()], "ABCD".index(tok.group("ans")))
    return keys


def _key_subject(residual: str, next_is_header: bool, warnings: list[str]) -> str | None:
    known = _known(residual)
    if known:
        return known
    slug = subject_slug(residual)
    for key, name in _KNOWN.items():
        if slug.endswith(key):
            warnings.append(
                f"Answer key: ignored unrecognised text before '{name}': {residual[:80]!r}"
            )
            return name
    if next_is_header:
        return residual
    warnings.append(f"Answer key: ignored unrecognised text {residual[:80]!r}")
    return None


def parse_pages(pages: Sequence[str]) -> ParsedBank:
    warnings: list[str] = []
    skipped: list[str] = []
    questions: list[ParsedQuestion] = []
    try:
        lines = _lines(pages)
        if not lines:
            warnings.append("No text found in the PDF (a scanned image needs a text layer)")
            return _bank(questions, warnings, skipped)
        blocks, key_lines = _split_questions(lines, warnings)
        if not blocks:
            warnings.append("No questions found (expected blocks like '1. Easy - Topic')")
        if key_lines is None:
            warnings.append("No 'Answer Key' section found; questions cannot be scored")
            keys = {}
        else:
            keys = _parse_key(key_lines, warnings)

        seen: set[tuple[str, int]] = set()
        display: dict[str, str] = {}
        for b in blocks:
            display.setdefault(subject_slug(b.subject), b.subject)
        for b in blocks:
            k = (subject_slug(b.subject), b.number)
            problem = _block_problem(b, k, seen, keys)
            seen.add(k)
            if problem:
                msg = f"{b.label}: {problem}"
                warnings.append(msg)
                skipped.append(msg)
                continue
            key_level, answer = keys[k]
            q_warnings: tuple[str, ...] = tuple(b.notes)
            if key_level != b.level:
                msg = (
                    f"{b.label}: level {_LEVEL_WORD[b.level]} in the question but "
                    f"{_LEVEL_WORD[key_level]} in the answer key; kept {_LEVEL_WORD[b.level]}"
                )
                warnings.append(msg)
                q_warnings += (msg,)
            questions.append(
                ParsedQuestion(
                    subject=b.subject,
                    number=b.number,
                    level=b.level,
                    topic=b.topic,
                    text=_join(b.stem),
                    options=tuple(_join(o) for o in b.options),
                    answer=answer,
                    warnings=q_warnings,
                )
            )
        for subj_slug, number in keys.keys() - seen:
            warnings.append(
                f"Answer key {display.get(subj_slug) or _KNOWN.get(subj_slug, subj_slug)} "
                f"#{number}: no question with this number"
            )
    except Exception as e:  # defensive: a parser bug must not turn into a 500 on upload
        warnings.append(f"Parser error: {type(e).__name__}: {e}")
    return _bank(questions, warnings, skipped)


def _block_problem(b: _Block, k: tuple[str, int], seen: set, keys: dict) -> str | None:
    if k in seen:
        return "duplicate question number in this subject"
    if len(b.options) != OPTION_COUNT:
        return f"{len(b.options)} options (expected {OPTION_COUNT})"
    if not _join(b.stem):
        return "empty question text"
    empty = [chr(65 + n) for n, o in enumerate(b.options) if not _join(o)]
    if empty:
        return f"empty option {', '.join(empty)}"
    if k not in keys:
        return "no answer in the answer key"
    return None


def _bank(questions: list[ParsedQuestion], warnings: list[str], skipped: list[str]) -> ParsedBank:
    by_subject: dict[str, int] = {}
    by_level: dict[str, int] = dict.fromkeys(LEVELS, 0)
    for q in questions:
        by_subject[q.subject] = by_subject.get(q.subject, 0) + 1
        by_level[q.level] += 1
    report = ParseReport(len(questions), by_subject, by_level, warnings, skipped)
    return ParsedBank(questions, report)


def extract_pages(data: bytes, *, max_pages: int = 400) -> list[str]:
    """Text of each page via pdfplumber. Unreadable or page-less PDF -> 422 PDF_UNREADABLE;
    more than `max_pages` pages -> 422 PDF_TOO_MANY_PAGES (checked before any extraction)."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            count = len(pdf.pages)
            if count > max_pages:
                raise AppError(
                    422,
                    "PDF_TOO_MANY_PAGES",
                    f"The PDF has {count} pages; the limit is {max_pages}",
                    {"pages": count, "max_pages": max_pages},
                )
            pages = []
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
                page.close()  # release pdfplumber's per-page object cache
    except AppError:
        raise
    except Exception as e:
        raise AppError(
            422, "PDF_UNREADABLE", "The PDF could not be read", {"reason": type(e).__name__}
        ) from e
    if not pages:
        raise AppError(422, "PDF_UNREADABLE", "The PDF has no pages")
    return pages

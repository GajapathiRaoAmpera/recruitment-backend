"""Local resume analyser (no AI): PDF text -> skills, CGPA, JD match. OCR only for scanned PDFs."""

import io
import re

import pdfplumber

MIN_TEXT = 40  # fewer characters than this means a scanned PDF: fall back to OCR

# canonical name -> aliases. shortcut: hand-made list, grow it from the 20-resume fixtures (AC-2)
SKILLS: dict[str, list[str]] = {
    "python": ["python"],
    "java": ["java"],
    "javascript": ["javascript", "js", "ecmascript"],
    "typescript": ["typescript"],
    "c": ["C"],  # case-sensitive below: "c" alone is too noisy
    "c++": ["c++", "cpp"],
    "c#": ["c#", "csharp"],
    "go": ["golang"],
    "rust": ["rust"],
    "sql": ["sql", "mysql", "postgresql", "postgres", "sqlite", "oracle", "pl/sql"],
    "mongodb": ["mongodb", "mongo"],
    "html": ["html", "html5"],
    "css": ["css", "css3"],
    "react": ["react", "reactjs", "react.js"],
    "angular": ["angular", "angularjs"],
    "vue": ["vue", "vuejs", "vue.js"],
    "node.js": ["node.js", "nodejs", "node"],
    "django": ["django"],
    "flask": ["flask"],
    "fastapi": ["fastapi"],
    "spring": ["spring", "spring boot", "springboot"],
    "docker": ["docker"],
    "kubernetes": ["kubernetes", "k8s"],
    "aws": ["aws", "amazon web services"],
    "azure": ["azure"],
    "gcp": ["gcp", "google cloud"],
    "git": ["git", "github", "gitlab"],
    "linux": ["linux", "unix"],
    "machine learning": ["machine learning", "ml"],
    "deep learning": ["deep learning"],
    "data structures": ["data structures", "dsa"],
    "pandas": ["pandas"],
    "numpy": ["numpy"],
    "tensorflow": ["tensorflow"],
    "pytorch": ["pytorch"],
    "excel": ["excel"],
    "power bi": ["power bi", "powerbi"],
    "tableau": ["tableau"],
}


def _pattern(alias: str) -> re.Pattern:
    # not glued to a word char or + # (so "java" skips "javascript", "c" skips "c++")
    flags = 0 if alias == "C" else re.IGNORECASE
    return re.compile(rf"(?<![\w+#]){re.escape(alias)}(?![\w+#])", flags)


_PATTERNS = {name: [_pattern(a) for a in aliases] for name, aliases in SKILLS.items()}


def extract_skills(text: str) -> list[str]:
    return sorted(n for n, pats in _PATTERNS.items() if any(p.search(text) for p in pats))


_LABEL = r"(?:cgpa|c\.g\.p\.a|gpa|cpi|sgpa)"
_NUM = r"(\d{1,2}(?:\.\d{1,2})?)"
_SCALE = r"(?:\s*(?:/|out of)\s*(10|4)(?:\.0+)?)?"
_CGPA = [
    re.compile(rf"{_LABEL}[^\d\n]{{0,25}}{_NUM}{_SCALE}", re.I),
    re.compile(rf"{_NUM}{_SCALE}\s*{_LABEL}", re.I),
    re.compile(rf"{_NUM}\s*/\s*(10)\b", re.I),
]


def extract_cgpa(text: str) -> float | None:
    """First plausible CGPA, on a 10-point scale (4-point values are converted)."""
    for pat in _CGPA:
        for m in pat.finditer(text):
            value, scale = float(m.group(1)), m.group(2)
            if scale == "4":
                value *= 2.5
            if 0 < value <= 10:
                return round(value, 2)
    return None


def jd_match(skills: list[str], jd_text: str) -> float | None:
    """% of the JD's skills the resume has; None when the JD names no known skill."""
    wanted = set(extract_skills(jd_text))
    if not wanted:
        return None
    return round(100 * len(wanted & set(skills)) / len(wanted), 2)


def _pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _ocr_text(data: bytes) -> str:
    # imported here: needs the tesseract + poppler binaries, which plain-text PDFs never touch
    import pytesseract
    from pdf2image import convert_from_bytes

    return "\n".join(pytesseract.image_to_string(img) for img in convert_from_bytes(data, dpi=200))


def extract_text(data: bytes) -> str:
    text = _pdf_text(data)
    return text if len(text.strip()) >= MIN_TEXT else _ocr_text(data)


def analyse(data: bytes, jd_text: str) -> dict:
    """Never raises: a bad file comes back as parse_status 'failed' so HR can still see the row."""
    try:
        text = extract_text(data)
    except Exception:  # corrupt PDF, or OCR binaries missing
        return {
            "text": None,
            "skills": [],
            "cgpa": None,
            "jd_match": None,
            "parse_status": "failed",
        }
    skills = extract_skills(text)
    return {
        "text": text,
        "skills": skills,
        "cgpa": extract_cgpa(text),
        "jd_match": jd_match(skills, jd_text),
        "parse_status": "ok" if text.strip() else "failed",
    }

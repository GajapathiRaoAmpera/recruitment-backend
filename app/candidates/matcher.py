"""Find which candidate a resume belongs to, from the resume's own text (file names are random)."""

import re
from pathlib import PurePath

_PHONE = re.compile(r"\+?\d[\d\s\-().]{8,}\d")


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def match_candidate(filename: str, text: str | None, candidates: list[dict]) -> object | None:
    """Candidate id, or None. A signal only counts if it points at exactly one candidate,
    so two students sharing a name or a family phone are never guessed between."""
    stem = _norm(PurePath(filename).stem)
    body = _norm(text or "")
    phones = {re.sub(r"\D", "", m)[-10:] for m in _PHONE.findall(text or "")}

    def sid_in_body(c) -> bool:
        sid = _norm(c["student_id"])
        return len(sid) >= 5 and re.search(rf"(?<![\w]){re.escape(sid)}(?![\w])", body) is not None

    signals = [
        lambda c: _norm(c["student_id"]) == stem,  # works when the file IS named by ID
        lambda c: bool(c["email"]) and _norm(c["email"]) in body,
        lambda c: (
            bool(c.get("phone"))
            and re.sub(r"\D", "", c["phone"])[-10:] in phones
            and len(re.sub(r"\D", "", c["phone"])) >= 10
        ),
        sid_in_body,
        lambda c: len(_norm(c["full_name"]).split()) >= 2 and _norm(c["full_name"]) in body,
    ]
    for hit in signals:
        found = [c["id"] for c in candidates if hit(c)]
        if len(found) == 1:
            return found[0]
    return None

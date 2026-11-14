"""What GET /attempt shows a student about a coding question's mode (no driver, ever).

Used by app/attempts/state_service.py so the attempt code only passes the row through.
"""

from app.execution.function_mode import values
from app.execution.function_mode.signature import load_signature
from app.execution.function_mode.starter import FUNCTION_LANGUAGES, starter_code
from app.execution.languages import LANGUAGES


def coding_fields(q: dict) -> dict:
    """Extra CodingQuestionOut fields for one row of coding_questions_for():
    mode, signature, starter_code, languages (no SQL in function mode), and samples with
    `input_display` ("nums = [1,2,3], target = 5"; the raw input for stdin questions)."""
    signature = load_signature(q.get("signature"))
    samples = [dict(s) for s in q.get("samples", [])]
    if signature is None:
        return {
            "mode": "stdin",
            "signature": None,
            "starter_code": None,
            "languages": [{"key": k, "name": v.name} for k, v in LANGUAGES.items()],
            "samples": [s | {"input_display": s["input"]} for s in samples],
        }
    return {
        "mode": "function",
        "signature": signature,
        "starter_code": starter_code(signature),
        "languages": [{"key": k, "name": LANGUAGES[k].name} for k in FUNCTION_LANGUAGES],
        "samples": [
            s | {"input_display": values.display_input(signature, s["input"])} for s in samples
        ],
    }

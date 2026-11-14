"""What GET /attempt shows a student about a SQL question: the schema, whether row order
matters, and for the 3 SAMPLE datasets only their rows as tables and the expected rows.

The setup script (settings + INSERT text) is never sent: a sample's `input` is "", its data is
in `tables`. Hidden datasets and the reference query are never selected for this view
(app/attempts/state_repo.coding_questions_for reads sample cases only).
"""

from app.execution.languages import LANGUAGES
from app.execution.sql_mode import datasets
from app.execution.sql_mode.runner import SQL_LANGUAGES, output_rows
from app.execution.sql_mode.script import parse_schema


def coding_fields(q: dict) -> dict:
    schema = parse_schema(q["sql_schema"], max_chars=None)
    return {
        "mode": "sql",
        "signature": None,
        "starter_code": None,
        "sql_schema": q["sql_schema"],
        "order_matters": bool(q["sql_order_matters"]),
        "languages": [{"key": k, "name": LANGUAGES[k].name} for k in SQL_LANGUAGES],
        "samples": [
            {
                "input": "",
                "expected": s["expected"],
                "input_display": None,
                "tables": datasets.tables(schema, s["input"]),
                "expected_rows": output_rows(s["expected"]),
            }
            for s in q.get("samples", [])
        ],
    }

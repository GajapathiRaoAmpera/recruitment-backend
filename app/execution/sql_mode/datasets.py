"""The 18 datasets of a SQL question, derived by code (never by the LLM) from its base rows.

- Dataset 1 (sample 1) is the base dataset as written, in its order.
- Datasets 2-18: per index, a fixed seed (SEED + index) drives Python's `random.Random`:
  every third index shuffles the order of all rows; the others keep a random subset of each
  table's rows (at least half, at most all but one), in a random order.
- Datasets are compared as text and a variant equal to an earlier one is skipped, so all 18
  are different; a base too small to give 18 different datasets is rejected.

Each dataset is stored as test_cases.input: one single-row INSERT per line, tables in schema
order. `tables()` turns a stored dataset back into rows for the student's sample view.
"""

import random

from app.execution.sql_mode.script import Row, Schema, Value, parse_dataset

SEED = 20_261_114  # fixed: the same base rows always give the same 18 datasets
TOTAL = 18
MAX_TRIES = 500


def render(rows: list[Row]) -> str:
    return "".join(r.sql + "\n" for r in rows)


def _by_table(schema: Schema, rows: list[Row]) -> list[list[Row]]:
    return [[r for r in rows if r.table == t.key] for t in schema.tables]


def _variant(index: int, groups: list[list[Row]]) -> list[Row]:
    rng = random.Random(SEED + index)
    out: list[Row] = []
    for rows in groups:
        rows = list(rows)
        if index % 3 == 1 or len(rows) < 2:  # row-order shuffle of every row
            rng.shuffle(rows)
        else:  # seeded subset, in a random order
            rows = rng.sample(rows, rng.randint((len(rows) + 1) // 2, len(rows) - 1))
        out += rows
    return out


def derive(schema: Schema, base: list[Row]) -> list[str]:
    """18 different dataset scripts: [base, 2 more samples, 15 hidden]. Raises ValueError
    when the base rows are too few to make 18 different datasets."""
    groups = _by_table(schema, base)
    datasets = [render(base)]
    seen = set(datasets)
    index = 0
    while len(datasets) < TOTAL:
        index += 1
        if index > MAX_TRIES:
            raise ValueError(
                f"the dataset has too few rows to make {TOTAL} different test datasets"
            )
        text = render(_variant(index, groups))
        if text not in seen:
            seen.add(text)
            datasets.append(text)
    return datasets


def reversed_order(schema: Schema, dataset: str) -> str:
    """The same rows inserted in reverse order (per table): used at generation time to check
    that the reference query's result does not depend on insertion order."""
    groups = _by_table(schema, parse_dataset(dataset, schema, max_chars=None))
    return render([r for g in groups for r in reversed(g)])


def tables(schema: Schema, dataset: str) -> dict[str, dict[str, list]]:
    """{table name: {"columns": [...], "rows": [[value, ...], ...]}} in schema order."""
    rows = parse_dataset(dataset, schema, max_chars=None)
    out: dict[str, dict[str, list]] = {}
    for t in schema.tables:
        values: list[list[Value]] = [list(r.values) for r in rows if r.table == t.key]
        out[t.label] = {"columns": list(t.columns), "rows": values}
    return out

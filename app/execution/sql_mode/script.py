"""Strict checks of what the LLM writes for a SQL question. Nothing here executes SQL.

- schema: 1-3 `CREATE TABLE name (...)` statements, nothing else (no `AS SELECT`, views,
  triggers, indexes, temp / qualified names).
- dataset: `INSERT INTO table [(every column)] VALUES (...), (...);` with literal values only
  (numbers, 'text', NULL, TRUE / FALSE), a value for every column, 1-15 rows per table. Each
  row is kept separately so datasets.py can shuffle and subset rows.
- reference query: exactly one statement, starting with SELECT or WITH, that reads only
  (no INSERT / UPDATE / DELETE / DDL), and is deterministic (no random(), no 'now').
- anywhere: no ATTACH / DETACH / PRAGMA / VACUUM, no dot-commands (a line starting with "."),
  and no `|` or line break inside a text literal (the output separator and row separator).

The lexer knows SQLite's literal and comment syntax only as far as needed to find statements,
keywords and literals; anything it does not understand is rejected, never guessed.
"""

import re
from dataclasses import dataclass

MAX_SCHEMA_CHARS = 3_000
MAX_DATASET_CHARS = 10_000
MAX_QUERY_CHARS = 2_000
MAX_TABLES = 3
MAX_COLUMNS = 10
MAX_ROWS_PER_TABLE = 15

_FORBIDDEN_ANYWHERE = {"ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX"}
_WRITES = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "CREATE",
    "ALTER",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "ANALYZE",
}
_NONDETERMINISTIC = {"RANDOM", "RANDOMBLOB", "CHANGES", "TOTAL_CHANGES", "LAST_INSERT_ROWID"}
_TABLE_CONSTRAINTS = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"}

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<line_comment>--[^\n]*)
  | (?P<block_comment>/\*.*?\*/)
  | (?P<str>'(?:[^']|'')*')
  | (?P<qident>"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\])
  | (?P<num>0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<op>\|\||<=|>=|<>|!=|==|<<|>>|[-+*/%<>=~&|(),;.?:@$!])
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class Token:
    kind: str  # str | qident | num | word | op
    text: str
    start: int
    end: int
    line_start: bool  # first token on its line

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "word" else ""


def tokenize(sql: str) -> list[Token]:
    """Raises ValueError on anything it cannot read (unterminated string / comment, stray
    characters) and on dot-commands."""
    tokens: list[Token] = []
    pos, at_line_start = 0, True
    while pos < len(sql):
        m = _TOKEN.match(sql, pos)
        if m is None:
            if sql.startswith("/*", pos):
                raise ValueError("unterminated /* comment")
            if sql[pos] in "'\"`[":
                raise ValueError(f"unterminated quote starting {sql[pos : pos + 20]!r}")
            raise ValueError(f"unexpected character {sql[pos]!r}")
        kind = m.lastgroup or ""
        if kind in ("space", "line_comment", "block_comment"):
            if "\n" in m.group():
                at_line_start = True
        else:
            if kind == "op" and m.group() == "." and at_line_start:
                raise ValueError("dot-commands (lines starting with '.') are not allowed")
            tokens.append(Token(kind, m.group(), m.start(), m.end(), at_line_start))
            at_line_start = False
        pos = m.end()
    return tokens


def split_statements(sql: str) -> list[tuple[str, list[Token]]]:
    """(statement text without the ';', its tokens) for every non-empty statement."""
    out, current = [], []
    for t in tokenize(sql):
        if t.kind == "op" and t.text == ";":
            if current:
                out.append((sql[current[0].start : current[-1].end], current))
            current = []
        else:
            current.append(t)
    if current:
        out.append((sql[current[0].start : current[-1].end], current))
    return out


def unquote(t: Token) -> str:
    if t.kind == "word":
        return t.text
    if t.kind == "qident":
        q, body = t.text[0], t.text[1:-1]
        return body if q == "[" else body.replace(q * 2, q)
    raise ValueError(f"expected a name, got {t.text!r}")


def _check_common(tokens: list[Token], what: str) -> None:
    for t in tokens:
        if t.upper in _FORBIDDEN_ANYWHERE:
            raise ValueError(f"{what} must not use {t.upper}")
        if t.kind == "str" and any(c in t.text for c in "|\n\r"):
            raise ValueError(f"{what}: text values must not contain '|' or line breaks")


def _close(tokens: list[Token], i: int) -> int:
    """Index of the ')' that closes the '(' at tokens[i]."""
    depth = 0
    for j in range(i, len(tokens)):
        if tokens[j].text == "(" and tokens[j].kind == "op":
            depth += 1
        elif tokens[j].text == ")" and tokens[j].kind == "op":
            depth -= 1
            if depth == 0:
                return j
    raise ValueError("unbalanced parentheses")


def _split_commas(tokens: list[Token]) -> list[list[Token]]:
    parts, current, depth = [], [], 0
    for t in tokens:
        if t.kind == "op" and t.text == "(":
            depth += 1
        elif t.kind == "op" and t.text == ")":
            depth -= 1
        if t.kind == "op" and t.text == "," and depth == 0:
            parts.append(current)
            current = []
        else:
            current.append(t)
    parts.append(current)
    return parts


# --- schema ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Table:
    name: str  # as written (may be quoted); used in generated INSERTs
    label: str  # unquoted, as shown to students
    key: str  # unquoted, case-folded: how SQLite matches names
    columns: tuple[str, ...]  # unquoted, in declaration order
    column_texts: tuple[str, ...]  # as written


@dataclass(frozen=True)
class Schema:
    text: str  # normalised: one statement per paragraph, each ending in ';'
    tables: tuple[Table, ...]

    def table(self, key: str) -> Table | None:
        return next((t for t in self.tables if t.key == key), None)


def parse_schema(sql: str, *, max_chars: int | None = MAX_SCHEMA_CHARS) -> Schema:
    """`max_chars` limits what the LLM wrote; the stored (normalised) schema passes None."""
    if max_chars is not None and len(sql) > max_chars:
        raise ValueError(f"schema is longer than {MAX_SCHEMA_CHARS} characters")
    statements = split_statements(sql)
    if not 1 <= len(statements) <= MAX_TABLES:
        raise ValueError(f"schema must have 1 to {MAX_TABLES} CREATE TABLE statements")
    tables: list[Table] = []
    for _, tokens in statements:
        _check_common(tokens, "schema")
        table = _parse_create(tokens)
        if any(t.key == table.key for t in tables):
            raise ValueError(f"table {table.name} is created twice")
        tables.append(table)
    return Schema("\n\n".join(text + ";" for text, _ in statements), tuple(tables))


def _parse_create(tokens: list[Token]) -> Table:
    words = [t.upper for t in tokens[:2]]
    if words != ["CREATE", "TABLE"]:
        raise ValueError("schema may only contain CREATE TABLE statements")
    i = 2
    if [t.upper for t in tokens[2:5]] == ["IF", "NOT", "EXISTS"]:
        i = 5
    if i + 1 >= len(tokens) or tokens[i + 1].text != "(":
        raise ValueError("CREATE TABLE must be followed by one table name and its columns")
    name_tok = tokens[i]
    name = unquote(name_tok)
    close = _close(tokens, i + 1)
    for t in tokens[close + 1 :]:  # only table options may follow: WITHOUT ROWID / STRICT
        if t.upper not in ("WITHOUT", "ROWID", "STRICT") and t.text != ",":
            raise ValueError(f"unexpected {t.text!r} after the column list of {name}")
    if any(t.upper == "SELECT" for t in tokens):
        raise ValueError("CREATE TABLE ... AS SELECT is not allowed")
    columns, texts = [], []
    for part in _split_commas(tokens[i + 2 : close]):
        if not part:
            raise ValueError(f"empty column definition in {name}")
        if part[0].upper in _TABLE_CONSTRAINTS:
            continue
        columns.append(unquote(part[0]))
        texts.append(part[0].text)
    if not 1 <= len(columns) <= MAX_COLUMNS:
        raise ValueError(f"table {name} must have 1 to {MAX_COLUMNS} columns")
    if len({c.casefold() for c in columns}) != len(columns):
        raise ValueError(f"table {name} has a repeated column name")
    return Table(name_tok.text, name, name.casefold(), tuple(columns), tuple(texts))


# --- dataset ---------------------------------------------------------------------------------

Value = int | float | str | None


@dataclass(frozen=True)
class Row:
    table: str  # Table.key
    sql: str  # one statement: INSERT INTO t (cols) VALUES (...);
    values: tuple[Value, ...]  # in the table's column order (for display)


def _literal(part: list[Token]) -> tuple[str, Value]:
    """(SQL text, Python value) of one literal value; raises for anything else."""
    sign = ""
    if len(part) == 2 and part[0].kind == "op" and part[0].text in "+-" and part[1].kind == "num":
        sign, part = part[0].text, part[1:]
    if len(part) != 1:
        raise ValueError("values must be literals: numbers, 'text', NULL, TRUE or FALSE")
    t = part[0]
    if t.kind == "num":
        if t.text.lower().startswith("0x"):
            raise ValueError("write numbers in decimal")
        text = sign + t.text
        num = float(text) if any(c in t.text for c in ".eE") else int(text)
        return text, num
    if sign:
        raise ValueError("values must be literals: numbers, 'text', NULL, TRUE or FALSE")
    if t.kind == "str":
        return t.text, t.text[1:-1].replace("''", "'")
    if t.upper == "NULL":
        return "NULL", None
    if t.upper in ("TRUE", "FALSE"):
        return t.upper, 1 if t.upper == "TRUE" else 0
    raise ValueError("values must be literals: numbers, 'text', NULL, TRUE or FALSE")


def parse_dataset(
    sql: str, schema: Schema, *, max_chars: int | None = MAX_DATASET_CHARS
) -> list[Row]:
    """Every row of every INSERT, in order, rewritten as one single-row INSERT that names
    every column. Raises ValueError for anything but literal INSERTs into the schema's tables.
    `max_chars` limits what the LLM wrote; stored datasets (already rewritten) pass None."""
    if max_chars is not None and len(sql) > max_chars:
        raise ValueError(f"dataset is longer than {MAX_DATASET_CHARS} characters")
    rows: list[Row] = []
    for _, tokens in split_statements(sql):
        _check_common(tokens, "dataset")
        rows += _parse_insert(tokens, schema)
    for table in schema.tables:
        n = sum(r.table == table.key for r in rows)
        if not 1 <= n <= MAX_ROWS_PER_TABLE:
            raise ValueError(f"table {table.name} needs 1 to {MAX_ROWS_PER_TABLE} rows, got {n}")
    return rows


def _parse_insert(tokens: list[Token], schema: Schema) -> list[Row]:
    if [t.upper for t in tokens[:2]] != ["INSERT", "INTO"] or len(tokens) < 4:
        raise ValueError("dataset may only contain INSERT INTO ... VALUES statements")
    table = schema.table(unquote(tokens[2]).casefold())
    if table is None:
        raise ValueError(f"INSERT into {tokens[2].text}, which the schema does not create")
    i = 3
    if tokens[i].text == "(":
        close = _close(tokens, i)
        names = [p for p in _split_commas(tokens[i + 1 : close])]
        if any(len(p) != 1 for p in names):
            raise ValueError(f"bad column list in INSERT into {table.name}")
        listed = [unquote(p[0]).casefold() for p in names]
        if sorted(listed) != sorted(c.casefold() for c in table.columns):
            raise ValueError(f"INSERT into {table.name} must give a value for every column")
        i = close + 1
    else:
        listed = [c.casefold() for c in table.columns]
    if i >= len(tokens) or tokens[i].upper != "VALUES":
        raise ValueError("dataset may only contain INSERT INTO ... VALUES statements")
    i += 1
    order = [listed.index(c.casefold()) for c in table.columns]  # table order -> listed position
    rows = []
    while True:
        if i >= len(tokens) or tokens[i].text != "(":
            raise ValueError(f"expected (values) in INSERT into {table.name}")
        close = _close(tokens, i)
        parsed = [_literal(p) for p in _split_commas(tokens[i + 1 : close])]
        if len(parsed) != len(table.columns):
            raise ValueError(
                f"a row of {table.name} has {len(parsed)} values for {len(table.columns)} columns"
            )
        in_table_order = [parsed[k] for k in order]
        sql = (
            f"INSERT INTO {table.name} ({', '.join(table.column_texts)}) VALUES "
            f"({', '.join(text for text, _ in in_table_order)});"
        )
        rows.append(Row(table.key, sql, tuple(v for _, v in in_table_order)))
        i = close + 1
        if i == len(tokens):
            return rows
        if tokens[i].text != ",":
            raise ValueError(f"unexpected {tokens[i].text!r} in INSERT into {table.name}")
        i += 1


# --- reference query -------------------------------------------------------------------------


def check_query(sql: str, *, order_matters: bool) -> str:
    """The reference query, stripped, ending in ';'. Raises ValueError when it is not exactly
    one deterministic, read-only SELECT / WITH statement."""
    if len(sql) > MAX_QUERY_CHARS:
        raise ValueError(f"reference_query is longer than {MAX_QUERY_CHARS} characters")
    statements = split_statements(sql)
    if len(statements) != 1:
        raise ValueError("reference_query must be exactly one statement")
    text, tokens = statements[0]
    _check_common(tokens, "reference_query")
    if tokens[0].upper not in ("SELECT", "WITH"):
        raise ValueError("reference_query must be a SELECT (or WITH ... SELECT)")
    for t in tokens:
        if t.upper in _WRITES:
            raise ValueError(f"reference_query must only read data (found {t.upper})")
        if t.upper in _NONDETERMINISTIC or (t.kind == "str" and t.text.lower() == "'now'"):
            raise ValueError(f"reference_query must give the same rows every run ({t.text})")
    if order_matters and not any(
        a.upper == "ORDER" and b.upper == "BY" for a, b in zip(tokens, tokens[1:], strict=False)
    ):
        raise ValueError("order_matters is true, so reference_query needs an ORDER BY")
    return text + ";"

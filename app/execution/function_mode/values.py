"""Canonical text form of function-mode values. Every driver parses and prints exactly this.

A test case's `input` is one line per parameter, in signature order, each line one value;
`expected` is the return value on one line. Values are compact JSON:

    int      -5                    32-bit
    long     9007199254740991      64-bit on output; inputs within +-(2**53 - 1) so that
                                   JavaScript (Number) reads them exactly
    double   2.5 / 1e-07           input: Python repr. Output: exactly 5 decimals, rounded
                                   half to even on the exact binary value ("0.33333");
                                   "-0.00000" is printed as "0.00000"
    bool     true / false
    string   "a\\"b"               JSON string; only " \\ and characters < U+0020 are escaped
                                   (\\b \\f \\n \\r \\t, others as lower-case \\u00xx);
                                   everything else is raw UTF-8
    int[]    [1,2,3]               no spaces; [] allowed
    string[] ["a","b"]
    int[][]  [[1,2],[3]]           rows may differ in length; [] and [[]] allowed

Text from HR (or a reply from the model) is parsed leniently as JSON (spaces are fine) and
stored in this canonical form, so the drivers only ever see canonical input.
"""

import json
import math
from typing import Any

from app.execution.function_mode.signature import Signature

INT_MIN, INT_MAX = -(2**31), 2**31 - 1
LONG_MIN, LONG_MAX = -(2**63), 2**63 - 1
SAFE_LONG = 2**53 - 1  # largest long an input may hold (JavaScript reads it exactly)
DOUBLE_DECIMALS = 5


def _no_constants(name: str):
    raise ValueError(f"{name} is not allowed")


def _loads(text: str) -> Any:
    try:
        return json.loads(text, parse_constant=_no_constants)
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON ({e.msg})") from None


def _is_int(v: Any) -> bool:
    return type(v) is int  # bool is a subclass of int in Python: excluded


def check_value(type_: str, v: Any, *, is_input: bool = True) -> Any:
    """Validate a JSON-decoded value for `type_`. Returns it (doubles as float).
    Raises ValueError with a readable reason."""
    if type_ == "int":
        if not _is_int(v) or not INT_MIN <= v <= INT_MAX:
            raise ValueError(f"expected int (32-bit whole number), got {_show(v)}")
        return v
    if type_ == "long":
        lo, hi = (-SAFE_LONG, SAFE_LONG) if is_input else (LONG_MIN, LONG_MAX)
        if not _is_int(v) or not lo <= v <= hi:
            rng = "within +-9007199254740991" if is_input else "64-bit"
            raise ValueError(f"expected long (whole number {rng}), got {_show(v)}")
        return v
    if type_ == "double":
        try:
            ok = (_is_int(v) or type(v) is float) and math.isfinite(float(v))
        except OverflowError:  # an integer too big for a double
            ok = False
        if not ok:
            raise ValueError(f"expected double (a finite number), got {_show(v)}")
        return float(v)
    if type_ == "bool":
        if type(v) is not bool:
            raise ValueError(f"expected bool (true or false), got {_show(v)}")
        return v
    if type_ == "string":
        if type(v) is not str:
            raise ValueError(f"expected string, got {_show(v)}")
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("string holds an invalid \\u escape (lone surrogate)") from None
        return v
    if type_ in ("int[]", "string[]", "int[][]"):
        if type(v) is not list:
            raise ValueError(f"expected {type_} (a JSON array), got {_show(v)}")
        inner = type_[:-2]
        return [check_value(inner, x, is_input=is_input) for x in v]
    raise ValueError(f"unknown type {type_!r}")


def _show(v: Any) -> str:
    text = json.dumps(v, ensure_ascii=False) if not isinstance(v, float) else repr(v)
    return text if len(text) <= 60 else text[:57] + "..."


def format_double(x: float) -> str:
    s = f"{x:.{DOUBLE_DECIMALS}f}"
    return s[1:] if s == "-0." + "0" * DOUBLE_DECIMALS else s


def format_value(type_: str, v: Any, *, is_input: bool = True) -> str:
    """Canonical text of an already checked value. Doubles: repr for inputs (exact), 5
    decimals for outputs (what every driver prints)."""
    if type_ in ("int", "long"):
        return str(v)
    if type_ == "double":
        return repr(float(v)) if is_input else format_double(v)
    if type_ == "bool":
        return "true" if v else "false"
    if type_ == "string":
        return json.dumps(v, ensure_ascii=False)
    inner = type_[:-2]
    return "[" + ",".join(format_value(inner, x, is_input=is_input) for x in v) + "]"


def _lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def canonical_input(sig: Signature, text: str) -> str:
    """HR text (one JSON value per line, per parameter) -> canonical stored input."""
    lines = _lines(text)
    if len(lines) != len(sig.parameters):
        raise ValueError(
            f"input needs {len(sig.parameters)} line(s), one value per parameter "
            f"({', '.join(sig.names)}), got {len(lines)}"
        )
    out = []
    for p, line in zip(sig.parameters, lines, strict=True):
        try:
            out.append(format_value(p.type, check_value(p.type, _loads(line))))
        except ValueError as e:
            raise ValueError(f"parameter {p.name}: {e}") from None
    return "\n".join(out) + "\n"


def canonical_args(sig: Signature, args: dict) -> str:
    """{"nums": [1,2], "target": 3} (a model reply) -> canonical stored input."""
    if not isinstance(args, dict):
        raise ValueError("each input must be a JSON object of parameter values")
    if set(args) != set(sig.names):
        raise ValueError(f"input keys must be exactly {sig.names}, got {sorted(args)}")
    out = []
    for p in sig.parameters:
        try:
            out.append(format_value(p.type, check_value(p.type, args[p.name])))
        except ValueError as e:
            raise ValueError(f"parameter {p.name}: {e}") from None
    return "\n".join(out) + "\n"


def canonical_expected(sig: Signature, text: str) -> str:
    """HR text of the return value -> canonical stored expected output."""
    lines = _lines(text)
    if len(lines) != 1:
        raise ValueError(f"expected output must be one {sig.return_type} value on one line")
    try:
        v = check_value(sig.return_type, _loads(lines[0]), is_input=False)
    except ValueError as e:
        raise ValueError(f"expected output: {e}") from None
    return format_value(sig.return_type, v, is_input=False) + "\n"


def display_input(sig: Signature, text: str) -> str:
    """Canonical stored input -> "nums = [1,2,3], target = 5" for the student's sample view.
    Falls back to the raw text if the stored case does not parse (it always should)."""
    lines = _lines(text)
    if len(lines) != len(sig.parameters):
        return text
    return ", ".join(f"{n} = {line}" for n, line in zip(sig.names, lines, strict=True))

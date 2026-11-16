"""Starter code: the empty function (or class) with the question's signature, per language.

Deterministic, generated from the signature only; there is no input-reading code in it. SQL
is not offered for function-mode questions. The headers / imports a student usually needs are
available without writing them (see drivers.py), so the stubs stay minimal:
C: stdio, stdlib, string, stdbool, limits, math. C++: <bits/stdc++.h> with `using namespace
std`. Java: java.util.*. Go: imports the student writes themselves, as usual.
"""

from app.execution.function_mode.signature import Signature

FUNCTION_LANGUAGES = ("python", "javascript", "c", "cpp", "java", "go")

_PY_DOC = {
    "int": "int",
    "long": "int",
    "double": "float",
    "bool": "bool",
    "string": "str",
    "int[]": "List[int]",
    "string[]": "List[str]",
    "int[][]": "List[List[int]]",
}
_JS = {
    "int": "number",
    "long": "number",
    "double": "number",
    "bool": "boolean",
    "string": "string",
    "int[]": "number[]",
    "string[]": "string[]",
    "int[][]": "number[][]",
}
_JAVA = {
    "int": "int",
    "long": "long",
    "double": "double",
    "bool": "boolean",
    "string": "String",
    "int[]": "int[]",
    "string[]": "String[]",
    "int[][]": "int[][]",
}
CPP_TYPES = {
    "int": "int",
    "long": "long long",
    "double": "double",
    "bool": "bool",
    "string": "string",
    "int[]": "vector<int>",
    "string[]": "vector<string>",
    "int[][]": "vector<vector<int>>",
}
GO_TYPES = {
    "int": "int",
    "long": "int64",
    "double": "float64",
    "bool": "bool",
    "string": "string",
    "int[]": "[]int",
    "string[]": "[]string",
    "int[][]": "[][]int",
}
C_SCALARS = {
    "int": "int",
    "long": "long long",
    "double": "double",
    "bool": "bool",
    "string": "char*",
}
C_RETURNS = {**C_SCALARS, "int[]": "int*", "string[]": "char**", "int[][]": "int**"}


def c_params(name: str, type_: str) -> list[str]:
    """One signature parameter as C parameters (LeetCode's C convention for arrays)."""
    if type_ in C_SCALARS:
        return [f"{C_SCALARS[type_]} {name}"]
    if type_ == "int[]":
        return [f"int* {name}", f"int {name}Size"]
    if type_ == "string[]":
        return [f"char** {name}", f"int {name}Size"]
    return [f"int** {name}", f"int {name}Size", f"int* {name}ColSize"]  # int[][]


C_RETURN_PARAMS = {
    "int[]": ["int* returnSize"],
    "string[]": ["int* returnSize"],
    "int[][]": ["int* returnSize", "int** returnColumnSizes"],
}
_C_RETURN_NOTE = {
    "string": "Return a NUL-terminated string (malloc it if you build it).",
    "int[]": "Return a malloc'ed array and set *returnSize to its length.",
    "string[]": "Return a malloc'ed array of strings and set *returnSize to its length.",
    "int[][]": "Return a malloc'ed array of malloc'ed rows; set *returnSize to the number of "
    "rows and *returnColumnSizes to a malloc'ed array of row lengths.",
}


def _python(sig: Signature) -> str:
    args = ", ".join(sig.names)
    types = ", ".join(f"{p.name}: {_PY_DOC[p.type]}" for p in sig.parameters)
    return (
        f"def {sig.function_name}({args}):\n    # {types} -> {_PY_DOC[sig.return_type]}\n    pass\n"
    )


def _javascript(sig: Signature) -> str:
    doc = [f" * @param {{{_JS[p.type]}}} {p.name}" for p in sig.parameters]
    return (
        "/**\n"
        + "\n".join(doc)
        + f"\n * @return {{{_JS[sig.return_type]}}}\n */\n"
        + f"function {sig.function_name}({', '.join(sig.names)}) {{\n    \n}}\n"
    )


def _c(sig: Signature) -> str:
    params = [c for p in sig.parameters for c in c_params(p.name, p.type)]
    params += C_RETURN_PARAMS.get(sig.return_type, [])
    note = _C_RETURN_NOTE.get(sig.return_type)
    head = f"// {note}\n" if note else ""
    return (
        f"{head}{C_RETURNS[sig.return_type]} {sig.function_name}({', '.join(params)}) "
        "{\n    \n}\n"
    )


def _cpp_param(name: str, type_: str) -> str:
    t = CPP_TYPES[type_]
    return f"{t}& {name}" if type_.endswith("[]") else f"{t} {name}"


def _cpp(sig: Signature) -> str:
    params = ", ".join(_cpp_param(p.name, p.type) for p in sig.parameters)
    return (
        "class Solution {\npublic:\n"
        f"    {CPP_TYPES[sig.return_type]} {sig.function_name}({params}) {{\n        \n    }}\n"
        "};\n"
    )


def _java(sig: Signature) -> str:
    params = ", ".join(f"{_JAVA[p.type]} {p.name}" for p in sig.parameters)
    return (
        "class Solution {\n"
        f"    public {_JAVA[sig.return_type]} {sig.function_name}({params}) {{\n        \n    }}\n"
        "}\n"
    )


def _go(sig: Signature) -> str:
    params = ", ".join(f"{p.name} {GO_TYPES[p.type]}" for p in sig.parameters)
    return f"func {sig.function_name}({params}) {GO_TYPES[sig.return_type]} {{\n    \n}}\n"


_BUILDERS = {
    "python": _python,
    "javascript": _javascript,
    "c": _c,
    "cpp": _cpp,
    "java": _java,
    "go": _go,
}


def starter_code(sig: Signature) -> dict[str, str]:
    """{language_key: stub} for every language a function-mode question allows."""
    return {lang: _BUILDERS[lang](sig) for lang in FUNCTION_LANGUAGES}

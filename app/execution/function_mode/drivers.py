"""Drivers: join the student's function with a fixed, per-language harness for Judge0.

The harness text lives in templates/<language>.tmpl and is never written by an LLM. Only a few
lines per question are generated here, deterministically from the signature: read each
parameter from its input line, call the function, print the result. The driver reads the
canonical case format (values.py) and prints the canonical output, so test cases, judging,
scoring and Judge0 batching stay exactly as for stdin questions.

The student's code always comes first, so a compile or runtime error on the student's line N
is reported as line N:
- Python, JavaScript, Java: the driver is appended after the code. Java also gets
  `import java.util.*;` prefixed on line 1 (same line, so nothing shifts).
- C, C++: a header prelude, then `#line 1`, then the code, then `#line 1 "driver.c(pp)"`.
- Go: the imports the driver needs go on the student's `package main` line (or are prefixed on
  line 1 when the code has no package clause); the driver is appended.

During the call the student's stdout is sent to stderr, so a debug print does not fail a case.

build_source() output must never reach the browser: callers send it to Judge0 only.
"""

import json
import re
from functools import cache
from pathlib import Path

from app.core.errors import AppError
from app.execution.function_mode.signature import Signature
from app.execution.function_mode.starter import (
    CPP_TYPES,
    FUNCTION_LANGUAGES,
    GO_TYPES,
)

_TEMPLATES = Path(__file__).parent / "templates"

C_PRELUDE = (
    "#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n#include <stdbool.h>\n"
    "#include <limits.h>\n#include <math.h>\n#line 1\n"
)
CPP_PRELUDE = "#include <bits/stdc++.h>\nusing namespace std;\n#line 1\n"
GO_HEADER = (
    'package main; import (drvJson "encoding/json"; drvIoutil "io/ioutil"; drvOs "os"; '
    'drvStrconv "strconv"; drvStrings "strings")'
)
JAVA_HEADER = "import java.util.*; "

_GO_PACKAGE = re.compile(r"^[ \t]*package[ \t]+\w+[ \t]*;?", re.MULTILINE)
_JAVA_PUBLIC_SOLUTION = re.compile(r"\bpublic(\s+(?:final\s+)?class\s+Solution\b)")


@cache
def template(language: str) -> str:
    return (_TEMPLATES / f"{language}.tmpl").read_text(encoding="utf-8")


def _fill(language: str, sig: Signature, call: str = "") -> str:
    return (
        template(language)
        .replace("@@CALL@@", call)
        .replace("@@FUNCTION@@", sig.function_name)
        .replace("@@PARAM_TYPES@@", json.dumps(sig.types))
        .replace("@@RETURN_TYPE@@", json.dumps(sig.return_type))  # as a code literal
        .replace("@@RETURN_NAME@@", sig.return_type)  # inside a message
    )


# --- per-language call code ------------------------------------------------------------------

_C_READ = {
    "int": ("int", "(int)drv_ll(&{p})"),
    "long": ("long long", "drv_ll(&{p})"),
    "double": ("double", "drv_double(&{p})"),
    "bool": ("bool", "drv_bool(&{p})"),
    "string": ("char *", "drv_str(&{p})"),
}
_C_PUT = {
    "int": "drv_put_int(drv_r);",
    "long": "drv_put_int(drv_r);",
    "double": "drv_put_double(drv_r);",
    "bool": "drv_put_bool(drv_r);",
    "string": "drv_put_str(drv_r);",
    "int[]": "drv_put_int_array(drv_r, drv_rn);",
    "string[]": "drv_put_str_array(drv_r, drv_rn);",
    "int[][]": "drv_put_int_matrix(drv_r, drv_rn, drv_rcols);",
}
_C_RET = {
    "int": "int",
    "long": "long long",
    "double": "double",
    "bool": "bool",
    "string": "char *",
    "int[]": "int *",
    "string[]": "char **",
    "int[][]": "int **",
}


def _c_call(sig: Signature) -> str:
    lines, args = [], []
    for i, p in enumerate(sig.parameters):
        v, ptr = f"drv_a{i}", f"drv_p{i}"
        lines.append(f"    const char *{ptr} = drv_line({i});")
        if p.type in _C_READ:
            ctype, expr = _C_READ[p.type]
            lines.append(f"    {ctype} {v} = {expr.format(p=ptr)};")
            args.append(v)
        elif p.type == "int[]":
            lines.append(f"    int {v}_n; int *{v} = drv_int_array(&{ptr}, &{v}_n);")
            args += [v, f"{v}_n"]
        elif p.type == "string[]":
            lines.append(f"    int {v}_n; char **{v} = drv_str_array(&{ptr}, &{v}_n);")
            args += [v, f"{v}_n"]
        else:  # int[][]
            lines.append(
                f"    int {v}_n; int *{v}_cols; int **{v} = drv_int_matrix(&{ptr}, &{v}_n, "
                f"&{v}_cols);"
            )
            args += [v, f"{v}_n", f"{v}_cols"]
    extra = {"int[]": ["&drv_rn"], "string[]": ["&drv_rn"], "int[][]": ["&drv_rn", "&drv_rcols"]}
    if sig.return_type in extra:
        lines.append("    int drv_rn = 0; int *drv_rcols = 0; (void)drv_rcols;")
        args += extra[sig.return_type]
    lines += [
        "    drv_begin_call();",
        f"    {_C_RET[sig.return_type]} drv_r = {sig.function_name}({', '.join(args)});",
        "    drv_end_call();",
        f"    {_C_PUT[sig.return_type]}",
    ]
    return "\n".join(lines)


_CPP_READ = {
    "int": "readInt",
    "long": "readLong",
    "double": "readDouble",
    "bool": "readBool",
    "string": "readString",
    "int[]": "readIntArray",
    "string[]": "readStringArray",
    "int[][]": "readIntMatrix",
}


def _cpp_call(sig: Signature) -> str:
    lines = []
    for i, p in enumerate(sig.parameters):
        lines.append(
            f"    {CPP_TYPES[p.type]} drvA{i} = drv::In(drv::line(drvLines, {i}))."
            f"{_CPP_READ[p.type]}();"
        )
    args = ", ".join(f"drvA{i}" for i in range(len(sig.parameters)))
    lines += [
        "    std::streambuf *drvBuf = std::cout.rdbuf(std::cerr.rdbuf());",
        "    FILE *drvStdout = stdout;",
        "    stdout = stderr;  // the student's prints go to stderr",
        "    Solution drvSol;",
        f"    {CPP_TYPES[sig.return_type]} drvR = drvSol.{sig.function_name}({args});",
        "    std::cout.flush();",
        "    std::fflush(stderr);",
        "    stdout = drvStdout;",
        "    std::cout.rdbuf(drvBuf);",
        '    std::string drvOut = drv::fmt(drvR) + "\\n";',
        "    std::fwrite(drvOut.data(), 1, drvOut.size(), stdout);",
        "    std::fflush(stdout);",
    ]
    return "\n".join(lines)


_JAVA = {  # type -> (Java type, reader, formatter)
    "int": ("int", "readInt", "fmtInt"),
    "long": ("long", "readLong", "fmtLong"),
    "double": ("double", "readDouble", "fmtDouble"),
    "bool": ("boolean", "readBool", "fmtBool"),
    "string": ("String", "readString", "fmtString"),
    "int[]": ("int[]", "readIntArray", "fmtIntArray"),
    "string[]": ("String[]", "readStringArray", "fmtStringArray"),
    "int[][]": ("int[][]", "readIntMatrix", "fmtIntMatrix"),
}


def _java_call(sig: Signature) -> str:
    lines = []
    for i, p in enumerate(sig.parameters):
        jtype, reader, _ = _JAVA[p.type]
        lines.append(f"        {jtype} drvA{i} = new DrvIn(drvLine(drvLines, {i})).{reader}();")
    args = ", ".join(f"drvA{i}" for i in range(len(sig.parameters)))
    rtype, _, fmt = _JAVA[sig.return_type]
    lines += [
        f"        {rtype} drvR;",
        "        System.setOut(System.err);  // the student's prints go to stderr",
        "        try {",
        f"            drvR = new Solution().{sig.function_name}({args});",
        "        } finally {",
        "            System.out.flush();",
        "            System.setOut(drvStdout);",
        "        }",
        f'        drvOut.print({fmt}(drvR) + "\\n");',
    ]
    return "\n".join(lines)


_GO_FMT = {
    "int": "drvFmtInt",
    "long": "drvFmtLong",
    "double": "drvFmtDouble",
    "bool": "drvFmtBool",
    "string": "drvFmtString",
    "int[]": "drvFmtIntArray",
    "string[]": "drvFmtStringArray",
    "int[][]": "drvFmtIntMatrix",
}


def _go_call(sig: Signature) -> str:
    lines = []
    for i, p in enumerate(sig.parameters):
        lines += [f"\tvar drvA{i} {GO_TYPES[p.type]}", f"\tdrvParse(drvLines, {i}, &drvA{i})"]
    args = ", ".join(f"drvA{i}" for i in range(len(sig.parameters)))
    lines += [
        "\tdrvStdout := drvOs.Stdout",
        "\tdrvOs.Stdout = drvOs.Stderr // the student's prints go to stderr",
        f"\tdrvR := {sig.function_name}({args})",
        "\tdrvOs.Stdout = drvStdout",
        f'\tdrvStdout.WriteString({_GO_FMT[sig.return_type]}(drvR) + "\\n")',
    ]
    return "\n".join(lines)


# --- joining ---------------------------------------------------------------------------------


def _ends_with_newline(code: str) -> str:
    return code if code.endswith("\n") else code + "\n"


def _go_source(sig: Signature, code: str) -> str:
    m = _GO_PACKAGE.search(code)
    if m and _only_comments_before(code[: m.start()]):  # Go requires the package clause first
        head = code[: m.start()] + GO_HEADER + code[m.end() :]
    else:  # no package clause: put it on line 1, before the student's first line
        head = GO_HEADER + "; " + code
    return _ends_with_newline(head) + _fill("go", sig, _go_call(sig))


def _only_comments_before(text: str) -> bool:
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return all(not ln.strip() or ln.strip().startswith("//") for ln in stripped.splitlines())


def build_source(language: str, sig: Signature, code: str) -> str:
    """Student code + driver, ready for Judge0. Line N of `code` stays line N."""
    if language not in FUNCTION_LANGUAGES:
        raise AppError(
            422,
            "LANGUAGE_NOT_ALLOWED",
            f"{language} can't be used for this question. Choose one of: "
            f"{', '.join(FUNCTION_LANGUAGES)}",
            {"allowed": list(FUNCTION_LANGUAGES)},
        )
    if language == "python":
        return _ends_with_newline(code) + _fill("python", sig)
    if language == "javascript":
        return _ends_with_newline(code) + _fill("javascript", sig)
    if language == "c":
        return C_PRELUDE + _ends_with_newline(code) + _fill("c", sig, _c_call(sig))
    if language == "cpp":
        return CPP_PRELUDE + _ends_with_newline(code) + _fill("cpp", sig, _cpp_call(sig))
    if language == "java":
        # `public class Solution` would have to live in Solution.java; drop the `public`
        code = _JAVA_PUBLIC_SOLUTION.sub(r"\1", code)
        return JAVA_HEADER + _ends_with_newline(code) + _fill("java", sig, _java_call(sig))
    return _go_source(sig, code)

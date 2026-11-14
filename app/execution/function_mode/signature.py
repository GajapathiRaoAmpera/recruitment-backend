"""The function signature of a function-mode question, and the small fixed type set.

Names are checked strictly because they are pasted into code in six languages:
- function_name: lowerCamelCase with at least one capital letter (e.g. twoSum), so it cannot
  clash with a keyword or a C library function (those are all lower case).
- parameter names: lowerCamelCase, not a keyword / predeclared name in any of the languages,
  not starting with "drv" (the drivers' prefix), and not one of the extra names C adds
  (<name>Size, <name>ColSize, returnSize, returnColumnSizes).
"""

import re
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ValueType = Literal["int", "long", "double", "bool", "string", "int[]", "string[]", "int[][]"]
VALUE_TYPES: tuple[str, ...] = get_args(ValueType)
Mode = Literal["stdin", "function"]

MAX_PARAMETERS = 6
MAX_NAME_CHARS = 40

_FUNCTION_NAME = re.compile(r"^[a-z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*$")
_PARAM_NAME = re.compile(r"^[a-z][a-zA-Z0-9]*$")

# Keywords, literals and predeclared names of Python, JavaScript, Java, C, C++ and Go, plus
# names the starter code / drivers rely on (types after `using namespace std`, package names).
RESERVED = frozenset(
    """
    and as assert async await break class continue def del elif else except finally for from
    global if import in is lambda nonlocal not or pass raise return try while with yield
    case catch const debugger default delete do enum export extends function instanceof new
    null switch this throw typeof var void let static implements interface package private
    protected public arguments eval undefined
    abstract boolean byte char double final float goto int long native short strictfp super
    synchronized throws transient volatile record sealed permits
    auto extern inline register restrict signed sizeof struct typedef union unsigned bool
    alignas alignof asm bitand bitor compl constexpr decltype explicit friend mutable namespace
    noexcept nullptr operator template typename using virtual xor concept requires
    chan defer fallthrough func go map range select type iota nil append cap close complex copy
    imag len make panic print println real recover error rune uintptr string true false
    vector std size main sys json fmt os math sort strings strconv bufio ioutil require process
    console module exports errno stdin stdout stderr assert isnan isinf
    returnSize returnColumnSizes
    """.split()
)


class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    type: ValueType

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _PARAM_NAME.match(v):
            raise ValueError(
                f"parameter name {v!r} must be lowerCamelCase letters and digits (e.g. nums)"
            )
        if v in RESERVED or v.startswith("drv"):
            raise ValueError(f"parameter name {v!r} is a reserved word in one of the languages")
        return v


class Signature(BaseModel):
    """{"function_name": "twoSum", "parameters": [{"name": "nums", "type": "int[]"}, ...],
    "return_type": "int[]"}"""

    model_config = ConfigDict(extra="forbid")

    function_name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    parameters: list[Parameter] = Field(min_length=1, max_length=MAX_PARAMETERS)
    return_type: ValueType

    @field_validator("function_name")
    @classmethod
    def _function_name(cls, v: str) -> str:
        if not _FUNCTION_NAME.match(v):
            raise ValueError(
                f"function_name {v!r} must be lowerCamelCase with at least two words, "
                "e.g. twoSum or maxProfit"
            )
        if v.startswith("drv"):
            raise ValueError(f"function_name {v!r} is reserved")
        return v

    @model_validator(mode="after")
    def _distinct(self) -> "Signature":
        names = [p.name for p in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("parameter names must be different")
        if self.function_name in names:
            raise ValueError("a parameter cannot have the function's name")
        taken = set(names)
        for n in names:  # C passes array lengths as extra parameters with these names
            if f"{n}Size" in taken or f"{n}ColSize" in taken:
                raise ValueError(f"parameter names {n!r} and {n}Size/{n}ColSize clash in C")
        return self

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.parameters]

    @property
    def types(self) -> list[str]:
        return [p.type for p in self.parameters]


def load_signature(raw) -> Signature | None:
    """From a DB row: jsonb may arrive as a dict or as text (asyncpg without a codec)."""
    if raw is None:
        return None
    if isinstance(raw, Signature):
        return raw
    if isinstance(raw, str | bytes):
        return Signature.model_validate_json(raw)
    return Signature.model_validate(raw)

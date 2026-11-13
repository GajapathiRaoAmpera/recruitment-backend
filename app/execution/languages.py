"""The one language map: our key -> Judge0 language id and run limits (FR-15).

Ids come from the running instance (GET /languages) of the pinned image
(infra/judge0, Judge0 1.13.1). Re-check them if that image is ever upgraded.
SQL runs on SQLite. Limits are first defaults per language; tune them with the team.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    cpu_time_s: float  # CPU seconds for one run
    wall_time_s: float  # real seconds for one run (catches sleeping / blocked programs)
    memory_kb: int


@dataclass(frozen=True)
class Language:
    key: str
    name: str
    judge0_id: int
    limits: Limits


_NATIVE = Limits(cpu_time_s=2, wall_time_s=5, memory_kb=256_000)
_SCRIPT = Limits(cpu_time_s=5, wall_time_s=10, memory_kb=256_000)
_JVM = Limits(cpu_time_s=4, wall_time_s=8, memory_kb=512_000)  # JVM start-up needs headroom

LANGUAGES: dict[str, Language] = {
    lang.key: lang
    for lang in (
        Language("python", "Python 3.8", 71, _SCRIPT),
        Language("javascript", "JavaScript (Node 12)", 63, _SCRIPT),
        Language("c", "C (GCC 9)", 50, _NATIVE),
        Language("cpp", "C++ (GCC 9)", 54, _NATIVE),
        Language("java", "Java 13", 62, _JVM),
        Language("go", "Go 1.13", 60, _NATIVE),
        Language("sql", "SQL (SQLite 3)", 82, _SCRIPT),
    )
}

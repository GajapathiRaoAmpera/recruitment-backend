"""Client for the self-hosted Judge0 (infra/judge0). Runs source code against stdin(s).

- Every run sends cpu / wall / memory limits (taken from the language map).
- The whole call is bounded by CLIENT_TIMEOUT_S; Judge0 being down or slow becomes an AppError.
- Judge0 runs the code in its workers: we submit a batch, then poll until every run is done.
- Output comes back base64: Judge0 answers 400 for plain-text output that is not valid UTF-8
  (compiler messages often are).
- Judging (comparing output, mapping to our statuses) is not done here; this returns what ran.
"""

import asyncio
import base64
import logging
import time
from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.core.errors import AppError
from app.execution.languages import LANGUAGES

log = logging.getLogger(__name__)

CLIENT_TIMEOUT_S = 15.0  # one call to run(), submit + wait for results
POLL_INTERVAL_S = 0.2
MAX_BATCH = 20  # Judge0's default MAX_SUBMISSION_BATCH_SIZE

# Judge0 status ids
IN_QUEUE, PROCESSING, ACCEPTED = 1, 2, 3  # Judge0 says "Accepted" for "ran fine"; it never judges
TIME_LIMIT_EXCEEDED, COMPILATION_ERROR = 5, 6
INTERNAL_ERROR = 13  # 7-12 are runtime errors (SIGSEGV ... NZEC)
_UNFINISHED = {IN_QUEUE, PROCESSING}

_FIELDS = "token,status,stdout,stderr,compile_output,message,time,memory,exit_code,exit_signal"


@dataclass(frozen=True)
class RunResult:
    status_id: int
    status: str  # Judge0's description, e.g. "Time Limit Exceeded"
    stdout: str
    stderr: str
    compile_output: str
    message: str
    time_s: float | None
    memory_kb: int | None
    exit_code: int | None


def _unavailable(why: str, **details) -> AppError:
    log.warning("Judge0 unavailable: %s %s", why, details)
    return AppError(
        503, "EXECUTION_UNAVAILABLE", "Code execution is unavailable. Try again.", details
    )


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _unb64(text: str | None) -> str:
    return base64.b64decode(text).decode("utf-8", "replace") if text else ""


def _parse(raw: dict) -> RunResult:
    time_s = raw.get("time")
    return RunResult(
        status_id=raw["status"]["id"],
        status=raw["status"]["description"],
        stdout=_unb64(raw.get("stdout")),
        stderr=_unb64(raw.get("stderr")),
        compile_output=_unb64(raw.get("compile_output")),
        message=_unb64(raw.get("message")),
        time_s=float(time_s) if time_s is not None else None,
        memory_kb=raw.get("memory"),
        exit_code=raw.get("exit_code"),
    )


class Judge0Client:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout_s: float = CLIENT_TIMEOUT_S,
        poll_interval_s: float = POLL_INTERVAL_S,
        transport: httpx.AsyncBaseTransport | None = None,  # tests pass a mock
    ):
        s = get_settings()
        self._base = (base_url or s.judge0_url).rstrip("/")
        token = s.judge0_auth_token if token is None else token
        self._headers = {"X-Auth-Token": token} if token else {}
        self._timeout_s, self._poll_s, self._transport = timeout_s, poll_interval_s, transport

    async def run(self, language: str, source: str, stdin: str = "") -> RunResult:
        return (await self.run_many(language, source, [stdin]))[0]

    async def run_many(self, language: str, source: str, stdins: list[str]) -> list[RunResult]:
        """Run `source` once per stdin, in parallel on the workers. Results keep stdin order."""
        lang = LANGUAGES[language]  # KeyError = programming error: validate the language upstream
        if not 1 <= len(stdins) <= MAX_BATCH:
            raise ValueError(f"run_many takes 1..{MAX_BATCH} inputs, got {len(stdins)}")
        lim = lang.limits
        body = {
            "submissions": [
                {
                    "language_id": lang.judge0_id,
                    "source_code": _b64(source),
                    "stdin": _b64(stdin),
                    "cpu_time_limit": lim.cpu_time_s,
                    "wall_time_limit": lim.wall_time_s,
                    "memory_limit": lim.memory_kb,
                }
                for stdin in stdins
            ]
        }
        try:
            async with asyncio.timeout(self._timeout_s):
                async with httpx.AsyncClient(
                    base_url=self._base,
                    headers=self._headers,
                    timeout=httpx.Timeout(self._timeout_s),
                    transport=self._transport,
                ) as http:
                    return await self._submit_and_wait(http, body)
        except TimeoutError as e:
            raise AppError(
                504, "EXECUTION_TIMEOUT", "Code execution took too long. Try again."
            ) from e
        except httpx.HTTPError as e:
            raise _unavailable("request failed", error=type(e).__name__) from e

    async def _submit_and_wait(self, http: httpx.AsyncClient, body: dict) -> list[RunResult]:
        r = await http.post("/submissions/batch", params={"base64_encoded": "true"}, json=body)
        if r.status_code != 201:
            raise _unavailable("submit rejected", judge0_status=r.status_code)
        tokens = [item["token"] for item in r.json()]

        started = time.monotonic()
        while True:
            r = await http.get(
                "/submissions/batch",
                params={"tokens": ",".join(tokens), "base64_encoded": "true", "fields": _FIELDS},
            )
            if r.status_code != 200:
                raise _unavailable("poll rejected", judge0_status=r.status_code)
            by_token = {item["token"]: item for item in r.json()["submissions"]}
            if len(by_token) == len(tokens) and all(
                by_token[t]["status"]["id"] not in _UNFINISHED for t in tokens
            ):
                return [_parse(by_token[t]) for t in tokens]
            log.debug("Judge0 batch pending after %.1fs", time.monotonic() - started)
            await asyncio.sleep(self._poll_s)

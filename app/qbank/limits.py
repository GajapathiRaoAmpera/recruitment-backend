"""Body-size guard for question-bank uploads (pure ASGI middleware).

FastAPI/Starlette spool a whole multipart body to disk before the route runs, so the route's own
size check comes too late to stop a huge upload. This middleware runs first, for POST/PUT under
/api/v1/question-banks only:
- a Content-Length above (QBANK_MAX_MB + 1) MB is rejected with 413 FILE_TOO_LARGE before any
  byte of the body is read (the extra MB covers multipart framing and the form fields);
- a body without Content-Length (chunked) is counted as it streams; past the limit reading stops
  and the response becomes 413 FILE_TOO_LARGE.
The route still enforces the exact QBANK_MAX_MB on the file itself. Set a reverse-proxy body limit
too (see the deployment notes).
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings

PREFIX = "/api/v1/question-banks"
METHODS = {"POST", "PUT"}


class BodyTooLarge(Exception):
    """Raised from receive() once a streamed body passes the limit."""


def limit_bytes() -> int:
    return (get_settings().qbank_max_mb + 1) * 1024 * 1024


def _error(status: int, code: str, message: str, details: dict | None = None) -> tuple[dict, bytes]:
    body = json.dumps({"error": {"code": code, "message": message, "details": details or {}}})
    data = body.encode()
    start = {
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(data)).encode()),
            (b"connection", b"close"),
        ],
    }
    return start, data


async def _send_error(send: Send, status: int, code: str, message: str, details=None) -> None:
    start, data = _error(status, code, message, details)
    await send(start)
    await send({"type": "http.response.body", "body": data})


def _too_large() -> tuple[int, str, str, dict]:
    mb = get_settings().qbank_max_mb
    return 413, "FILE_TOO_LARGE", f"The PDF must be {mb} MB or smaller", {"max_mb": mb}


class QbankBodyLimit:
    def __init__(self, app: ASGIApp, prefix: str = PREFIX):
        self.app = app
        self.prefix = prefix

    def _applies(self, scope: Scope) -> bool:
        if scope["type"] != "http" or scope.get("method") not in METHODS:
            return False
        path = scope.get("path", "")
        return path == self.prefix or path.startswith(self.prefix + "/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return
        limit = limit_bytes()
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                await _send_error(send, 400, "BAD_REQUEST", "Invalid Content-Length header")
                return
            if declared > limit:
                await _send_error(send, *_too_large())  # the body is never read
                return
            # declared size is within the limit; the server enforces Content-Length itself

        received = 0
        exceeded = False
        started = False

        async def counted() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                # whatever the app made of the aborted read (400 parse error, ...) becomes 413
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await _send_error(send, *_too_large())
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, guarded_send)
        except BodyTooLarge:
            if not started:
                await _send_error(send, *_too_large())

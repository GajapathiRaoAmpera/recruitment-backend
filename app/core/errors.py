"""Every error leaves the API as {"error": {"code", "message", "details"}} (02-architecture.md)."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

log = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        self.status, self.code, self.message, self.details = status, code, message, details or {}


def _body(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, e: AppError):
        return _body(e.status, e.code, e.message, e.details)

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, e: HTTPException):
        return _body(e.status_code, f"HTTP_{e.status_code}", str(e.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, e: RequestValidationError):
        errors = [{"loc": err["loc"], "msg": err["msg"]} for err in e.errors()]
        return _body(422, "VALIDATION_ERROR", "Request is invalid", {"errors": errors})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, e: Exception):
        log.exception("unhandled error")  # never leak internals to the client
        return _body(500, "INTERNAL_ERROR", "Something went wrong")

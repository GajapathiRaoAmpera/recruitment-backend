from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from app.core.errors import AppError, install_error_handlers
from app.main import app

t = FastAPI()  # throwaway app so test routes don't touch the real one
install_error_handlers(t)


@t.get("/_test/app-error")
async def _raise_app_error():
    raise AppError(409, "CONFLICT_X", "nope", {"id": 1})


@t.get("/_test/boom")
async def _raise_unhandled():
    raise RuntimeError("secret internals")


@t.get("/_test/validate")
async def _validate(n: int = Query()):
    return {"n": n}


client = TestClient(t, raise_server_exceptions=False)


def test_healthz():
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_app_error_shape():
    r = client.get("/_test/app-error")
    assert r.status_code == 409
    assert r.json() == {"error": {"code": "CONFLICT_X", "message": "nope", "details": {"id": 1}}}


def test_not_found_shape():
    r = client.get("/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "HTTP_404"


def test_validation_shape():
    r = client.get("/_test/validate?n=abc")
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unhandled_does_not_leak():
    r = client.get("/_test/boom")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR" and "secret" not in r.text


def test_database_url_forced_to_asyncpg():
    from app.core.config import Settings

    for raw in ("postgresql://u:p@h:5432/d", "postgres://u:p@h:5432/d"):
        assert Settings(database_url=raw).database_url == "postgresql+asyncpg://u:p@h:5432/d"
    already = "postgresql+asyncpg://u:p@h:5432/d"
    assert Settings(database_url=already).database_url == already


def test_openapi_marks_file_fields_binary():
    from app.main import app

    schemas = app.openapi()["components"]["schemas"]
    sheet = next(v for k, v in schemas.items() if "import_candidates" in k)["properties"]["sheet"]
    assert sheet["format"] == "binary"

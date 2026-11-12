from fastapi import APIRouter, FastAPI

from app.admin.router import router as admin_router
from app.attempts.router import router as attempts_router
from app.auth.router import router as auth_router
from app.candidates.router import router as candidates_router
from app.core.errors import install_error_handlers
from app.drives.router import router as drives_router
from app.execution.coding_questions.router import router as coding_questions_router
from app.execution.results.router import router as results_router
from app.execution.router import router as execution_router
from app.execution.test_config.router import router as test_config_router
from app.papers.router import router as papers_router
from app.qbank.limits import QbankBodyLimit
from app.qbank.router import router as qbank_router

app = FastAPI(title="Ampera Campus Hire API", version="0.1.0")
install_error_handlers(app)
# rejects oversize question-bank uploads before their body is spooled to disk
app.add_middleware(QbankBodyLimit)

api = APIRouter(prefix="/api/v1")  # feature routers are included here


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    return {"status": "ok"}


api.include_router(auth_router)
api.include_router(attempts_router)
api.include_router(admin_router)
api.include_router(candidates_router)
api.include_router(drives_router)
api.include_router(papers_router)
api.include_router(qbank_router)
api.include_router(execution_router)
api.include_router(coding_questions_router)
api.include_router(test_config_router)
api.include_router(results_router)
app.include_router(api)


def _openapi() -> dict:
    """Swagger UI shows file fields as a text box unless they carry `format: binary`."""
    if app.openapi_schema is None:
        schema = FastAPI.openapi(app)

        def tag(node):
            if isinstance(node, dict):
                if node.get("contentMediaType") == "application/octet-stream":
                    node["format"] = "binary"
                for v in node.values():
                    tag(v)
            elif isinstance(node, list):
                for v in node:
                    tag(v)

        tag(schema)
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _openapi

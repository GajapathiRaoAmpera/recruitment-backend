from fastapi import APIRouter, FastAPI

from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.core.errors import install_error_handlers
from app.execution.router import router as execution_router

app = FastAPI(title="Ampera Campus Hire API", version="0.1.0")
install_error_handlers(app)

api = APIRouter(prefix="/api/v1")  # feature routers are included here


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    return {"status": "ok"}


api.include_router(auth_router)
api.include_router(admin_router)
api.include_router(execution_router)
app.include_router(api)

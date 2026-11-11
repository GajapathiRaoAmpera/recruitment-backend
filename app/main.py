from fastapi import APIRouter, FastAPI

from app.core.errors import install_error_handlers

app = FastAPI(title="Ampera Campus Hire API", version="0.1.0")
install_error_handlers(app)

api = APIRouter(prefix="/api/v1")  # feature routers are included here


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    return {"status": "ok"}


app.include_router(api)

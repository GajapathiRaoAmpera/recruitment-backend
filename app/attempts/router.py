from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.attempts import service
from app.attempts.schemas import StartIn, StartOut
from app.auth.entry import Entry, current_entry
from app.core.db import get_session

router = APIRouter(tags=["attempt"])


@router.post("/entry/start", response_model=StartOut)
async def entry_start(
    body: StartIn,
    who: Entry = Depends(current_entry),
    session: AsyncSession = Depends(get_session),
):
    """Needs the entry token. A repeat call returns the same attempt with the same deadline."""
    return await service.start(session, who, body.round_id)

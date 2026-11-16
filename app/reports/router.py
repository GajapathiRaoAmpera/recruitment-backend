from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import StaffUser
from app.auth.security import require_role
from app.core.db import get_session
from app.reports import service

router = APIRouter(prefix="/drives", tags=["reports"])


@router.get(
    "/{drive_id}/report.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def drive_report(
    drive_id: UUID,
    user: StaffUser = Depends(require_role("hr")),
    session: AsyncSession = Depends(get_session),
):
    """Closed drive -> one branded PDF documenting the whole drive (409 while it is still running).
    HR gets only their own drives; no DOB, phone, email, answers or recordings are included."""
    name, pdf = await service.drive_report(session, user, drive_id)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )

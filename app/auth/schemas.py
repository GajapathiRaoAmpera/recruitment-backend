from typing import Literal
from uuid import UUID

from pydantic import BaseModel

Role = Literal["hr", "super_admin"]


class StaffUser(BaseModel):
    """The signed-in HR/Admin user; also the GET /me response."""

    id: UUID
    email: str
    full_name: str
    role: Role

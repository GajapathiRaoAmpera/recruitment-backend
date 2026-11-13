from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, EmailStr, StringConstraints

from app.auth.schemas import Role

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class UserCreate(BaseModel):
    email: EmailStr
    full_name: Name
    role: Role = "hr"
    send_email: bool = True  # False: return a one-time invite link instead of emailing it


class UserPatch(BaseModel):
    full_name: Name | None = None
    role: Role | None = None
    is_active: bool | None = None


class UserOut(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    created_at: datetime


class UserCreated(UserOut):
    # One-time sign-in link: a credential. Only in this response; never logged or audited.
    invite_link: str | None = None


class UserPage(BaseModel):
    items: list[UserOut]
    next_cursor: str | None = None

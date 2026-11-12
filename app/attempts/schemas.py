from uuid import UUID

from pydantic import BaseModel


class StartIn(BaseModel):
    round_id: UUID


class StartOut(BaseModel):
    attempt_token: str
    attempt_id: UUID

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def audit(
    session: AsyncSession,
    actor_id: UUID,
    action: str,
    entity: str,
    entity_id: object,
    diff: dict | None = None,
) -> None:
    """Record an HR/Admin mutation. Call inside the mutation's transaction, before commit."""
    await session.execute(
        text(
            "insert into audit_logs (actor_id, action, entity, entity_id, diff) "
            "values (:actor, :action, :entity, :entity_id, cast(:diff as jsonb))"
        ),
        {
            "actor": actor_id,
            "action": action,
            "entity": entity,
            "entity_id": str(entity_id),
            "diff": json.dumps(diff, default=str) if diff is not None else None,
        },
    )

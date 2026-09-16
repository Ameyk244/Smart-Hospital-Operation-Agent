"""Data access for the grounding ledger (concepts 22, 23).

Why it exists: raw persistence for `SessionGroundedEntity`. Deliberately
dumb — no policy here. `app/agent/grounding.py` is where "may this tool call
proceed" is decided; this module only records and checks facts.

What calls it: `app/agent/grounding.py` exclusively — no tool or route
should import this directly, so the policy layer stays the single place
grounding rules can change.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.agent import SessionGroundedEntity


class GroundingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def expose(self, session_id: str, entity_type: str, codes: list[str]) -> None:
        if not codes:
            return
        stmt = (
            pg_insert(SessionGroundedEntity)
            .values(
                [
                    {"session_id": session_id, "entity_type": entity_type, "entity_code": code}
                    for code in codes
                ]
            )
            .on_conflict_do_nothing(
                index_elements=[
                    SessionGroundedEntity.session_id,
                    SessionGroundedEntity.entity_type,
                    SessionGroundedEntity.entity_code,
                ]
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def is_grounded(self, session_id: str, entity_type: str, code: str) -> bool:
        stmt = select(SessionGroundedEntity.id).where(
            SessionGroundedEntity.session_id == session_id,
            SessionGroundedEntity.entity_type == entity_type,
            SessionGroundedEntity.entity_code == code,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_grounded(self, session_id: str, entity_type: str | None = None) -> list[str]:
        stmt = select(SessionGroundedEntity.entity_code).where(
            SessionGroundedEntity.session_id == session_id
        )
        if entity_type is not None:
            stmt = stmt.where(SessionGroundedEntity.entity_type == entity_type)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

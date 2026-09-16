"""Persistent preference memory (concepts 37, 38, 41).

Why it exists: the *only* data access path for the explicit
`remember_preference`/`forget_preference`/`list_preferences` tools. Nothing
else writes to this table — preferences are never inferred by the agent
loop, only stated and later recalled on purpose.

What calls it: `app/agent/tools/memory_tools.py`.
"""

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.agent import Preference


class PreferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def remember(self, session_id: str, key: str, value: str) -> Preference:
        """Upsert on (session_id, key) — remembering the same key twice
        overwrites, it doesn't duplicate."""
        stmt = (
            pg_insert(Preference)
            .values(session_id=session_id, key=key, value=value)
            .on_conflict_do_update(
                index_elements=[Preference.session_id, Preference.key],
                set_={"value": value},
            )
            .returning(Preference)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one()

    async def forget(self, session_id: str, key: str) -> bool:
        stmt = delete(Preference).where(
            Preference.session_id == session_id, Preference.key == key
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    async def list_for_session(self, session_id: str) -> list[Preference]:
        stmt = (
            select(Preference)
            .where(Preference.session_id == session_id)
            .order_by(Preference.key)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

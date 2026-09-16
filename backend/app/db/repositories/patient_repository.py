"""Patient data access.

Why it exists: patient lookup by name is deliberately fuzzy (`ILIKE`) so
"show patient John Doe" and `search_patients("john")` can both return
multiple plausible matches — the ambiguity is what makes grounding matter
(see docs/ARCHITECTURE.md §6).

What calls it: deterministic parser commands, `app/agent/tools/search_tools.py`.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.hospital import Patient


class PatientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_code(self, code: str) -> Patient | None:
        result = await self._session.execute(
            select(Patient).where(Patient.code == code).execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def search_by_name(self, query: str, limit: int = 10) -> list[Patient]:
        stmt = (
            select(Patient)
            .where(Patient.name.ilike(f"%{query}%"))
            .order_by(Patient.name)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

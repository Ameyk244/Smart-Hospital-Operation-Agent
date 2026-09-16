"""Read-only access to reference data: departments, staff, rooms.

Why it exists: these entities are rarely mutated in this project (no tool or
command changes them) but are looked up constantly — by search filters, by
grounding display, by seed data. Grouped in one repository because each one
individually would be a single two-method class.

What calls it: `app/execution/commands/*` (validating a department/staff code
exists before using it), `app/agent/tools/search_tools.py`.

Fails: returns `None` on an unknown code; callers decide whether that's a
validation error (400) or a grounding rejection.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.hospital import Department, Room, Staff


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_departments(self) -> list[Department]:
        result = await self._session.execute(
            select(Department).order_by(Department.code).execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def get_department_by_code(self, code: str) -> Department | None:
        result = await self._session.execute(
            select(Department).where(Department.code == code).execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_staff_by_code(self, code: str) -> Staff | None:
        result = await self._session.execute(
            select(Staff).where(Staff.code == code).execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_room_by_code(self, code: str) -> Room | None:
        result = await self._session.execute(
            select(Room).where(Room.code == code).execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

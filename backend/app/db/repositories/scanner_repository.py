"""Scanner data access.

Why it exists: scanner search/availability is used by both the deterministic
`list scanners` command (also reachable through the agent's `execute_command`)
and the `get_scanner_availability` tool — one query surface for both.

What calls it: `app/execution/commands/*`, `app/agent/tools/search_tools.py`,
`app/agent/tools/observation_tools.py`.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.hospital import Department, Room, Scanner, ScannerStatus, ScannerType


class ScannerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_code(self, code: str) -> Scanner | None:
        result = await self._session.execute(
            select(Scanner)
            .where(Scanner.code == code)
            .options(selectinload(Scanner.room))
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def search(
        self,
        *,
        scanner_type: ScannerType | None = None,
        status: ScannerStatus | None = None,
        department_code: str | None = None,
    ) -> list[Scanner]:
        stmt = select(Scanner).options(selectinload(Scanner.room)).execution_options(populate_existing=True)
        if scanner_type is not None:
            stmt = stmt.where(Scanner.type == scanner_type)
        if status is not None:
            stmt = stmt.where(Scanner.status == status)
        if department_code is not None:
            stmt = stmt.join(Room).join(Department).where(Department.code == department_code)
        stmt = stmt.order_by(Scanner.code)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_compatible_available(self, scanner_type: ScannerType) -> list[Scanner]:
        """Scanners of the right modality that are currently available — the
        candidate set for `reschedule_appointment`'s new_scanner_id."""
        return await self.search(scanner_type=scanner_type, status=ScannerStatus.AVAILABLE)

"""Ward and bed data access.

Why it exists: one query surface for the ward/bed reads (`list wards`,
`list beds`, the agent's `search_beds`) and for the bed writes. Like the
appointment repository, the mutation methods here do not enforce business
rules; `app/execution/commands/ward_commands.py` validates first.

What calls it: `app/execution/commands/ward_commands.py`.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.hospital import Bed, BedStatus, Patient, QueueEntry, QueueStatus, Ward


class WardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_wards(self) -> list[Ward]:
        result = await self._session.execute(
            select(Ward)
            .options(selectinload(Ward.department), selectinload(Ward.beds))
            .order_by(func.length(Ward.code), Ward.code)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())


class BedRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_query(self):
        return (
            select(Bed)
            .options(selectinload(Bed.ward), selectinload(Bed.patient))
            .execution_options(populate_existing=True)
        )

    async def get_by_code(self, code: str, *, for_update: bool = False) -> Bed | None:
        stmt = self._base_query().where(Bed.code == code)
        if for_update:
            stmt = stmt.with_for_update(of=Bed)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def search(
        self, *, status: BedStatus | None = None, ward_code: str | None = None
    ) -> list[Bed]:
        stmt = self._base_query()
        if status is not None:
            stmt = stmt.where(Bed.status == status)
        if ward_code is not None:
            stmt = stmt.where(Bed.ward.has(code=ward_code))
        result = await self._session.execute(stmt.order_by(func.length(Bed.code), Bed.code))
        return list(result.scalars().all())

    async def occupied_by(self, patient: Patient) -> Bed | None:
        result = await self._session.execute(
            self._base_query().where(Bed.patient_id == patient.id)
        )
        return result.scalar_one_or_none()

    async def admit(
        self, bed: Bed, patient: Patient, entry: QueueEntry, occupied_since: datetime
    ) -> None:
        bed.status = BedStatus.OCCUPIED
        bed.patient = patient
        bed.occupied_since = occupied_since
        entry.status = QueueStatus.ADMITTED
        entry.bed = bed
        await self._session.flush()

    async def release(self, bed: Bed, entry: QueueEntry, discharged_at: datetime) -> None:
        bed.status = BedStatus.CLEANING
        bed.patient = None
        bed.occupied_since = None
        entry.status = QueueStatus.DISCHARGED
        entry.discharged_at = discharged_at
        await self._session.flush()

    async def mark_clean(self, bed: Bed) -> None:
        bed.status = BedStatus.AVAILABLE
        await self._session.flush()

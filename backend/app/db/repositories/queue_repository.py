"""Emergency-queue data access.

Why it exists: the queue listing, the "waiting entry" and "admitted entry"
lookups the bed commands need, and the latest-entry lookup the patient
journey is derived from.

What calls it: `app/execution/commands/ward_commands.py`,
`app/execution/commands/queue_commands.py`.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.hospital import Bed, Patient, QueueEntry, QueueStatus, QueueUrgency


class QueueRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_query(self):
        return (
            select(QueueEntry)
            .options(
                selectinload(QueueEntry.patient),
                selectinload(QueueEntry.bed).selectinload(Bed.ward),
            )
            .execution_options(populate_existing=True)
        )

    async def search(
        self, *, urgency: QueueUrgency | None = None, status: QueueStatus | None = None
    ) -> list[QueueEntry]:
        stmt = self._base_query()
        if urgency is not None:
            stmt = stmt.where(QueueEntry.urgency == urgency)
        if status is not None:
            stmt = stmt.where(QueueEntry.status == status)
        result = await self._session.execute(stmt.order_by(QueueEntry.urgency, QueueEntry.arrived_at))
        return list(result.scalars().all())

    async def get_waiting(self, patient: Patient) -> QueueEntry | None:
        result = await self._session.execute(
            self._base_query()
            .where(QueueEntry.patient_id == patient.id, QueueEntry.status == QueueStatus.WAITING)
            .order_by(QueueEntry.arrived_at)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_admitted_in(self, bed: Bed) -> QueueEntry:
        result = await self._session.execute(
            self._base_query().where(
                QueueEntry.bed_id == bed.id, QueueEntry.status == QueueStatus.ADMITTED
            )
        )
        return result.scalar_one()

    async def get_latest(self, patient: Patient) -> QueueEntry | None:
        result = await self._session.execute(
            self._base_query()
            .where(QueueEntry.patient_id == patient.id)
            .order_by(QueueEntry.arrived_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

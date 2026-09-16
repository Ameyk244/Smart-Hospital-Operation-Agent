"""Appointment data access — the entity most tools and commands touch.

Why it exists: centralizes every appointment query/mutation behind one
surface so `CommandRunner`'s execution functions (the single canonical path
for both the deterministic parser and the agent's write tools, see
docs/ARCHITECTURE.md §1-2) never hand-write SQL. Mutation methods here do
not enforce business rules (e.g. "scanner must be AVAILABLE and the right
modality") — that belongs to `app/execution/commands/`, which calls these
methods only after its own validation. Keeping "can I write this row" (here)
separate from "should I write this row" (execution layer) is what makes the
command layer unit-testable without a database.

What calls it: `app/execution/commands/*`, `app/agent/tools/search_tools.py`,
`app/agent/tools/observation_tools.py`.

Fails: mutation methods raise `ValueError` if given an unknown `code`, since
by the time a mutation is attempted the caller (CommandRunner) is expected to
have already resolved and validated the entity — an unknown code reaching
here is a programming error, not a user-facing validation case.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.hospital import Appointment, AppointmentStatus, ScannerType


class AppointmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_query(self):
        # populate_existing(): within one request/agent-round, the same
        # session is reused across multiple repository calls (e.g. a search
        # tool followed later by a write tool's own lookup). Without this,
        # SQLAlchemy's identity map would silently return an already-loaded
        # (possibly stale) object instead of the row this query just read —
        # exactly the kind of staleness a multi-tool agent loop must not hit.
        return (
            select(Appointment)
            .options(
                selectinload(Appointment.patient),
                selectinload(Appointment.department),
                selectinload(Appointment.scanner),
                selectinload(Appointment.staff),
            )
            .execution_options(populate_existing=True)
        )

    async def get_by_code(self, code: str) -> Appointment | None:
        stmt = self._base_query().where(Appointment.code == code)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def search(
        self,
        *,
        status: AppointmentStatus | None = None,
        appointment_type: ScannerType | None = None,
        patient_code: str | None = None,
        department_code: str | None = None,
        scanner_code: str | None = None,
        on_or_after: datetime | None = None,
        limit: int = 25,
    ) -> list[Appointment]:
        stmt = self._base_query()
        if status is not None:
            stmt = stmt.where(Appointment.status == status)
        if appointment_type is not None:
            stmt = stmt.where(Appointment.appointment_type == appointment_type)
        if patient_code is not None:
            stmt = stmt.join(Appointment.patient).where(
                Appointment.patient.has(code=patient_code)
            )
        if department_code is not None:
            stmt = stmt.where(Appointment.department.has(code=department_code))
        if scanner_code is not None:
            stmt = stmt.where(Appointment.scanner.has(code=scanner_code))
        if on_or_after is not None:
            stmt = stmt.where(Appointment.scheduled_start >= on_or_after)
        stmt = stmt.order_by(Appointment.scheduled_start).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_next(self, *, patient_code: str | None = None) -> Appointment | None:
        """The soonest scheduled appointment at/after now, optionally scoped
        to one patient. Backs "show the next appointment"."""
        results = await self.search(
            status=AppointmentStatus.SCHEDULED,
            patient_code=patient_code,
            on_or_after=datetime.now(timezone.utc),
            limit=1,
        )
        return results[0] if results else None

    async def reassign_scanner(
        self, code: str, *, new_scanner_id: int, new_start: datetime, new_end: datetime
    ) -> Appointment:
        appointment = await self.get_by_code(code)
        if appointment is None:
            raise ValueError(f"No appointment with code {code!r}")
        appointment.scanner_id = new_scanner_id
        appointment.scheduled_start = new_start
        appointment.scheduled_end = new_end
        appointment.status = AppointmentStatus.SCHEDULED
        await self._session.flush()
        return appointment

    async def set_status(self, code: str, status: AppointmentStatus) -> Appointment:
        appointment = await self.get_by_code(code)
        if appointment is None:
            raise ValueError(f"No appointment with code {code!r}")
        appointment.status = status
        await self._session.flush()
        return appointment

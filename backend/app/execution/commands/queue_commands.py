"""Emergency-queue commands: the queue listing and the patient journey.

Why it exists: backs "show emergency queue" / "show journey <name>" in the
deterministic grammar and the agent's `search_queue` / `get_patient_journey`
tools. The journey has no table: it is derived on read from the patient's
latest queue entry, that entry's bed, and the patient's appointments.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.hospital import Appointment, AppointmentStatus, QueueEntry, QueueStatus
from app.db.repositories.appointment_repository import AppointmentRepository
from app.db.repositories.patient_repository import PatientRepository
from app.db.repositories.queue_repository import QueueRepository
from app.execution.commands.base import CommandError, register
from app.execution.commands.coercion import coerce_queue_status, coerce_queue_urgency
from app.schemas.hospital import JourneyOut, QueueEntryOut


@register("list_emergency_queue")
async def list_emergency_queue(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    entries = await QueueRepository(session).search(
        urgency=coerce_queue_urgency(args.get("urgency")),
        status=coerce_queue_status(args.get("status")),
    )
    return [QueueEntryOut.model_validate(e).model_dump(mode="json") for e in entries]


def _stages(entry: QueueEntry, appointments: list[Appointment]) -> list[tuple[str, bool, Any, str | None]]:
    """(stage, reached, at, detail) in journey order. The scan is the patient's
    first live appointment since arrival; it counts as reached once COMPLETED."""
    scan = next(
        (
            a
            for a in appointments
            if a.status != AppointmentStatus.CANCELLED and a.scheduled_start >= entry.arrived_at
        ),
        None,
    )
    scanned = scan is not None and scan.status == AppointmentStatus.COMPLETED
    return [
        ("ARRIVED", True, entry.arrived_at, entry.urgency.value),
        ("TRIAGED", entry.triaged_at is not None, entry.triaged_at, None),
        (
            "BED_ASSIGNED",
            entry.bed is not None,
            entry.bed.occupied_since if entry.status == QueueStatus.ADMITTED else None,
            f"{entry.bed.code} ({entry.bed.ward.name})" if entry.bed else None,
        ),
        (
            "SCAN",
            scanned,
            scan.scheduled_start if scanned else None,
            f"{scan.appointment_type.value} {scan.code} ({scan.status.value})" if scan else None,
        ),
        ("DISCHARGED", entry.discharged_at is not None, entry.discharged_at, None),
    ]


@register("patient_journey")
async def patient_journey(session: AsyncSession, args: dict[str, Any]) -> dict:
    patient_code = args.get("patient_code")
    query = args.get("query")
    patients = PatientRepository(session)
    if patient_code:
        patient = await patients.get_by_code(patient_code)
        if patient is None:
            raise ValueError(f"No patient with code {patient_code!r}")
    elif query:
        matches = await patients.search_by_name(query)
        if not matches:
            raise ValueError(f"No patient matching {query!r}")
        if len(matches) > 1:
            codes = ", ".join(p.code for p in matches)
            raise CommandError(
                f"{len(matches)} patients match {query!r} ({codes}); pass a patient_code",
                category="ambiguous_patient",
            )
        patient = matches[0]
    else:
        raise CommandError("patient_code or query is required", category="invalid_argument")

    appointments = await AppointmentRepository(session).search(patient_code=patient.code, limit=50)
    entry = await QueueRepository(session).get_latest(patient)
    stages = _stages(entry, appointments) if entry else []
    reached = [stage for stage, done, _, _ in stages if done]
    journey = {
        "patient": patient,
        "stages": [{"stage": s, "at": at, "detail": detail} for s, _, at, detail in stages],
        "current_stage": reached[-1] if reached else "NONE",
        "appointments": appointments,
    }
    return JourneyOut.model_validate(journey, from_attributes=True).model_dump(mode="json")

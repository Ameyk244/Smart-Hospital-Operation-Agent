"""Appointment search and the one mutating command in the system.

Why it exists: `reassign_scanner` is the canonical implementation of "move
an appointment to a (possibly new) scanner" — the operation the
`reschedule_appointment` agent tool describes from the user's point of view.
The tool does not re-implement this; it builds a
`Command("reassign_scanner", ...)` with grounded arguments
(see docs/ARCHITECTURE.md §2 and `app/agent/tools/action_tools.py`).

Business rules enforced here (not by any caller):
- the target scanner must exist,
- its modality must match the appointment's `appointment_type` (an MRI
  appointment cannot go on a CT scanner),
- it must currently be AVAILABLE.
A violation raises `CommandError`, which `CommandRunner` turns into a
`CommandResult(success=False, ...)` instead of a raw DB write.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.hospital import AppointmentStatus, ScannerStatus
from app.db.repositories.appointment_repository import AppointmentRepository
from app.db.repositories.scanner_repository import ScannerRepository
from app.execution.commands.base import CommandError, register
from app.execution.commands.coercion import coerce_appointment_status, coerce_scanner_type
from app.schemas.hospital import AppointmentOut


@register("search_appointments")
async def search_appointments(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    status = coerce_appointment_status(args.get("status"))
    appointment_type = coerce_scanner_type(args.get("appointment_type"))
    appointments = await AppointmentRepository(session).search(
        status=status,
        appointment_type=appointment_type,
        patient_code=args.get("patient_code"),
        department_code=args.get("department_code"),
        scanner_code=args.get("scanner_code"),
        limit=int(args.get("limit", 25)),
    )
    return [AppointmentOut.model_validate(a).model_dump(mode="json") for a in appointments]


@register("list_delayed_appointments")
async def list_delayed_appointments(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    appointment_type = coerce_scanner_type(args.get("appointment_type"))
    appointments = await AppointmentRepository(session).search(
        status=AppointmentStatus.DELAYED, appointment_type=appointment_type, limit=50
    )
    return [AppointmentOut.model_validate(a).model_dump(mode="json") for a in appointments]


@register("reassign_scanner")
async def reassign_scanner(session: AsyncSession, args: dict[str, Any]) -> dict:
    appointment_code = args.get("appointment_code")
    scanner_code = args.get("scanner_code")
    if not appointment_code or not scanner_code:
        raise CommandError(
            "appointment_code and scanner_code are both required", category="invalid_argument"
        )

    appt_repo = AppointmentRepository(session)
    scanner_repo = ScannerRepository(session)

    appointment = await appt_repo.get_by_code(appointment_code)
    if appointment is None:
        raise ValueError(f"No appointment with code {appointment_code!r}")

    scanner = await scanner_repo.get_by_code(scanner_code)
    if scanner is None:
        raise ValueError(f"No scanner with code {scanner_code!r}")

    if scanner.type != appointment.appointment_type:
        raise CommandError(
            f"Scanner {scanner_code} is a {scanner.type.value} scanner, but the "
            f"appointment needs {appointment.appointment_type.value}",
            category="modality_mismatch",
        )
    if scanner.status != ScannerStatus.AVAILABLE:
        raise CommandError(
            f"Scanner {scanner_code} is not available (status: {scanner.status.value})",
            category="scanner_unavailable",
        )

    new_start_raw = args.get("new_start")
    if new_start_raw is not None:
        new_start = datetime.fromisoformat(new_start_raw)
    else:
        new_start = appointment.scheduled_start
    duration = appointment.scheduled_end - appointment.scheduled_start
    new_end = new_start + (duration if duration > timedelta(0) else timedelta(minutes=45))

    updated = await appt_repo.reassign_scanner(
        appointment_code, new_scanner_id=scanner.id, new_start=new_start, new_end=new_end
    )
    return AppointmentOut.model_validate(updated).model_dump(mode="json")

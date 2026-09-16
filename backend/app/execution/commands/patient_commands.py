"""Patient and per-patient appointment commands.

Why it exists: backs "show patient <name>" / "show next appointment" in the
deterministic grammar and `search_patients` / `get_next_appointment`-style
agent tools.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.appointment_repository import AppointmentRepository
from app.db.repositories.patient_repository import PatientRepository
from app.execution.commands.base import CommandError, register
from app.schemas.hospital import AppointmentOut, PatientOut


@register("search_patients")
async def search_patients(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    query = args.get("query")
    if not query:
        raise CommandError("query is required", category="invalid_argument")
    patients = await PatientRepository(session).search_by_name(query)
    return [PatientOut.model_validate(p).model_dump(mode="json") for p in patients]


@register("list_patient_appointments")
async def list_patient_appointments(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    patient_code = args.get("patient_code")
    if not patient_code:
        raise CommandError("patient_code is required", category="invalid_argument")
    patient = await PatientRepository(session).get_by_code(patient_code)
    if patient is None:
        raise ValueError(f"No patient with code {patient_code!r}")
    appointments = await AppointmentRepository(session).search(patient_code=patient_code, limit=50)
    return [AppointmentOut.model_validate(a).model_dump(mode="json") for a in appointments]


@register("show_next_appointment")
async def show_next_appointment(session: AsyncSession, args: dict[str, Any]) -> dict | None:
    patient_code = args.get("patient_code")
    appointment = await AppointmentRepository(session).get_next(patient_code=patient_code)
    if appointment is None:
        return None
    return AppointmentOut.model_validate(appointment).model_dump(mode="json")

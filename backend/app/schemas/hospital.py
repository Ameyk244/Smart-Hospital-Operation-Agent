"""JSON-safe output shapes for hospital entities.

Why it exists: `CommandRunner` results, agent tool results, and FastAPI
responses all need to serialize ORM objects the same way — one set of
Pydantic models (not three hand-rolled dict builders) keeps that consistent
and gives every entity a schema-validated shape as soon as it leaves the
repository layer.

What calls it: `app/execution/commands/*` (building `CommandResult.data`),
`app/api/routes/*` (FastAPI `response_model`).
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.db.models.hospital import AppointmentStatus, ScannerStatus, ScannerType


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str


class StaffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str
    role: str


class ScannerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str
    type: ScannerType
    status: ScannerStatus


class PatientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    mrn: str
    name: str
    date_of_birth: date


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    patient: PatientOut
    department: DepartmentOut
    scanner: ScannerOut | None
    staff: StaffOut | None
    appointment_type: ScannerType
    scheduled_start: datetime
    scheduled_end: datetime
    status: AppointmentStatus
    notes: str | None

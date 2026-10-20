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

from app.db.models.hospital import (
    AppointmentStatus,
    BedStatus,
    QueueStatus,
    QueueUrgency,
    ScannerStatus,
    ScannerType,
)


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


class WardRefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str


class WardOut(WardRefOut):
    department: DepartmentOut
    bed_count: int
    occupied_count: int


class PatientRefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str


class BedPatientOut(PatientRefOut):
    mrn: str


class BedOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    ward: WardRefOut
    status: BedStatus
    patient: BedPatientOut | None
    occupied_since: datetime | None


class BedRefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    ward: WardRefOut


class QueueEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    patient: PatientRefOut
    urgency: QueueUrgency
    status: QueueStatus
    arrived_at: datetime
    triaged_at: datetime | None
    bed: BedRefOut | None
    discharged_at: datetime | None
    wait_minutes: int


class JourneyStageOut(BaseModel):
    stage: str
    at: datetime | None
    detail: str | None


class JourneyOut(BaseModel):
    patient: PatientRefOut
    stages: list[JourneyStageOut]
    current_stage: str
    appointments: list[AppointmentOut]

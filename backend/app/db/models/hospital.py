"""Hospital operations domain models.

Why it exists: the synthetic operational data the deterministic system and
the agent both operate on. Every entity has an internal integer `id` (DB
concern only, never leaves the repository layer) and a human-readable
`code` (e.g. "APT-1001", "SCN-3") that is the *only* identifier ever passed
to tools, the API, or the grounding registry. This mirrors how a real
system exposes stable business keys instead of raw surrogate keys, and it
makes grounding tests read naturally: a fabricated `"APT-9999"` is
obviously not a code any repository query could have produced.

What calls it: `app/db/repositories/*`, `app/seed/seed_data.py`, Alembic.
"""

import enum
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ScannerType(str, enum.Enum):
    MRI = "MRI"
    CT = "CT"
    XRAY = "XRAY"


class ScannerStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    MAINTENANCE = "MAINTENANCE"


class AppointmentStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    DELAYED = "DELAYED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))

    staff: Mapped[list["Staff"]] = relationship(back_populates="department")
    rooms: Mapped[list["Room"]] = relationship(back_populates="department")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="department")


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(60))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))

    department: Mapped["Department"] = relationship(back_populates="staff")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="staff")


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))

    department: Mapped["Department"] = relationship(back_populates="rooms")
    scanners: Mapped[list["Scanner"]] = relationship(back_populates="room")


class Scanner(Base):
    __tablename__ = "scanners"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[ScannerType] = mapped_column(Enum(ScannerType, name="scanner_type"))
    status: Mapped[ScannerStatus] = mapped_column(
        Enum(ScannerStatus, name="scanner_status"), default=ScannerStatus.AVAILABLE
    )
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"))

    room: Mapped["Room"] = relationship(back_populates="scanners")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="scanner")


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    mrn: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    date_of_birth: Mapped[date] = mapped_column(Date)

    appointments: Mapped[list["Appointment"]] = relationship(back_populates="patient")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    scanner_id: Mapped[int | None] = mapped_column(ForeignKey("scanners.id"), nullable=True)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    appointment_type: Mapped[ScannerType] = mapped_column(Enum(ScannerType, name="scanner_type"))
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, name="appointment_status"), default=AppointmentStatus.SCHEDULED
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    patient: Mapped["Patient"] = relationship(back_populates="appointments")
    department: Mapped["Department"] = relationship(back_populates="appointments")
    scanner: Mapped["Scanner | None"] = relationship(back_populates="appointments")
    staff: Mapped["Staff | None"] = relationship(back_populates="appointments")

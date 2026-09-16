"""Re-exports every model so `Base.metadata` is fully populated for Alembic
autogenerate with a single `import app.db.models`."""

from app.db.models.agent import (
    AgentEvent,
    AgentSession,
    ConversationMessage,
    EventStatus,
    MessageRole,
    Preference,
    SessionGroundedEntity,
)
from app.db.models.hospital import (
    Appointment,
    AppointmentStatus,
    Department,
    Patient,
    Room,
    Scanner,
    ScannerStatus,
    ScannerType,
    Staff,
)

__all__ = [
    "AgentEvent",
    "AgentSession",
    "Appointment",
    "AppointmentStatus",
    "ConversationMessage",
    "Department",
    "EventStatus",
    "MessageRole",
    "Patient",
    "Preference",
    "Room",
    "Scanner",
    "ScannerStatus",
    "ScannerType",
    "SessionGroundedEntity",
    "Staff",
]

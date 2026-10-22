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
from app.db.models.audit import AuditRecord
from app.db.models.hospital import (
    Appointment,
    AppointmentStatus,
    Bed,
    BedStatus,
    Department,
    Patient,
    QueueEntry,
    QueueStatus,
    QueueUrgency,
    Room,
    Scanner,
    ScannerStatus,
    ScannerType,
    Staff,
    Ward,
)
from app.db.models.scheduler import (
    Alert,
    AlertSeverity,
    ScheduledTask,
    TaskKind,
    WatchCondition,
)

__all__ = [
    "AgentEvent",
    "AgentSession",
    "Alert",
    "AlertSeverity",
    "Appointment",
    "AppointmentStatus",
    "AuditRecord",
    "Bed",
    "BedStatus",
    "ConversationMessage",
    "Department",
    "EventStatus",
    "MessageRole",
    "Patient",
    "Preference",
    "QueueEntry",
    "QueueStatus",
    "QueueUrgency",
    "Room",
    "ScheduledTask",
    "Scanner",
    "ScannerStatus",
    "ScannerType",
    "SessionGroundedEntity",
    "Staff",
    "TaskKind",
    "Ward",
    "WatchCondition",
]

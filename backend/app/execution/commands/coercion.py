"""Shared arg-to-enum coercion for command handlers.

Why it exists: `Command.args` is a plain `dict[str, Any]` (it has to be —
both the parser and the LLM tool layer produce raw strings/JSON-ish values,
not Python enums). Several commands need the same "is this a valid
ScannerType/ScannerStatus/AppointmentStatus" check with the same error
shape; factored out once instead of copied into each command module.
"""

from typing import Any, TypeVar

from app.db.models.hospital import (
    AppointmentStatus,
    BedStatus,
    QueueStatus,
    QueueUrgency,
    ScannerStatus,
    ScannerType,
)
from app.db.models.scheduler import WatchCondition
from app.execution.commands.base import CommandError

E = TypeVar("E")


def _coerce_enum(value: Any, enum_cls: type[E], label: str) -> E | None:
    if value is None:
        return None
    try:
        return enum_cls(value)  # type: ignore[call-arg]
    except ValueError as exc:
        valid = [m.value for m in enum_cls]  # type: ignore[attr-defined]
        raise CommandError(
            f"{value!r} is not a valid {label} (expected one of {valid})",
            category="invalid_argument",
        ) from exc


def coerce_scanner_type(value: Any) -> ScannerType | None:
    return _coerce_enum(value, ScannerType, "scanner type")


def coerce_scanner_status(value: Any) -> ScannerStatus | None:
    return _coerce_enum(value, ScannerStatus, "scanner status")


def coerce_appointment_status(value: Any) -> AppointmentStatus | None:
    return _coerce_enum(value, AppointmentStatus, "appointment status")


def coerce_bed_status(value: Any) -> BedStatus | None:
    return _coerce_enum(value, BedStatus, "bed status")


def coerce_queue_urgency(value: Any) -> QueueUrgency | None:
    return _coerce_enum(value, QueueUrgency, "queue urgency")


def coerce_queue_status(value: Any) -> QueueStatus | None:
    return _coerce_enum(value, QueueStatus, "queue status")


def coerce_watch_condition(value: Any) -> WatchCondition | None:
    return _coerce_enum(value, WatchCondition, "watch condition")

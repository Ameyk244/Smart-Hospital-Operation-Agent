"""Scheduled watches and the alerts they raise (docs/SCHEDULER.md).

A watch names one condition over an existing read command; it holds ids, enums
and numbers only. An alert's `summary` is templated from those, never free text.
"""

import enum
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class TaskKind(str, enum.Enum):
    WATCH = "WATCH"


class WatchCondition(str, enum.Enum):
    QUEUE_WAIT_OVER = "QUEUE_WAIT_OVER"
    FREE_BEDS_UNDER = "FREE_BEDS_UNDER"
    DELAYED_APPOINTMENTS_OVER = "DELAYED_APPOINTMENTS_OVER"


class AlertSeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    kind: Mapped[TaskKind] = mapped_column(Enum(TaskKind, name="task_kind"), default=TaskKind.WATCH)
    condition: Mapped[WatchCondition] = mapped_column(Enum(WatchCondition, name="watch_condition"))
    threshold: Mapped[int] = mapped_column(Integer)
    scope_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    period_seconds: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("scheduled_tasks.id"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(40))
    severity: Mapped[AlertSeverity] = mapped_column(Enum(AlertSeverity, name="alert_severity"))
    summary: Mapped[str] = mapped_column(String(200))
    entity_codes: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped["ScheduledTask"] = relationship()

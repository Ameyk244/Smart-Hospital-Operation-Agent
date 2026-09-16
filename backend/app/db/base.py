"""Declarative base for all ORM models.

Why it exists: SQLAlchemy 2.0's typed mapping style needs one shared
`DeclarativeBase` subclass so Alembic's autogenerate can discover every
model via a single `Base.metadata`.

What calls it: every module in `app/db/models/`, plus
`alembic/env.py` (target_metadata).
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """Adds a `created_at` column defaulted at the DB level.

    Why: several tables (agent_events, conversation_messages,
    session_grounded_entities, preferences) need an audit-style creation
    timestamp. A mixin keeps that consistent instead of retyping the column.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=None, nullable=False
    )

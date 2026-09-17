"""Agent-facing persistence models.

Why it exists: these tables back the three *separate* memory concepts from
docs/ARCHITECTURE.md §7 (conversation state, persistent preferences —
LangGraph's own checkpoint tables are owned by `langgraph-checkpoint-postgres`
and intentionally not modeled here) plus the grounding ledger (§6) and the
observability trace (§9). Keeping them in one module makes the memory/
observability boundary easy to audit: if it isn't in this file, it isn't one
of these three concepts.

What calls it: `app/agent/grounding.py`, `app/agent/memory/*`,
`app/observability/tracing.py`, via their respective repositories.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow


def new_session_id() -> str:
    return str(uuid.uuid4())


class AgentSession(Base):
    """Session identity (concept 34). Everything else in this file hangs off
    `session_id`, a client-visible UUID string, not a raw DB integer."""

    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_session_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MessageRole(str, enum.Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    TOOL = "TOOL"
    SYSTEM = "SYSTEM"


class ConversationMessage(Base, TimestampMixin):
    """Short-term conversation state (concept 35): the durable record of a
    session's transcript. Distinct from LangGraph's checkpoint state, which
    tracks *graph execution*, not the human-readable conversation."""

    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    role: Mapped[MessageRole] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)

    session: Mapped["AgentSession"] = relationship()


class Preference(Base, TimestampMixin):
    """Persistent, explicit-only memory (concepts 37, 38, 41): written and
    removed *only* by the `remember_preference`/`forget_preference` tools —
    never inferred or auto-written by the agent loop itself."""

    __tablename__ = "preferences"
    __table_args__ = (UniqueConstraint("session_id", "key", name="uq_preference_session_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text)

    session: Mapped["AgentSession"] = relationship()


class SessionGroundedEntity(Base, TimestampMixin):
    """The grounding ledger (concepts 22, 23): every entity `code` a search
    or read tool has exposed to a given session. Write tools may only
    reference a `code` present here for that session — checked in
    `app/agent/grounding.py`, not by prompting the model to behave."""

    __tablename__ = "session_grounded_entities"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "entity_type", "entity_code", name="uq_grounded_entity"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_code: Mapped[str] = mapped_column(String(20))

    session: Mapped["AgentSession"] = relationship()


class EventStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    REJECTED = "REJECTED"


class AgentEvent(Base, TimestampMixin):
    """The observability trace (concepts 48-50): one row per action-level
    decision inside the LangGraph run (model round, tool call, grounding
    check, round/limit termination). Deliberately action-level only — never stores
    model chain-of-thought/private reasoning, only what was requested,
    validated, executed, and the outcome."""

    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    round_num: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String(60))
    tool_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    arguments_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[EventStatus] = mapped_column(String(20))
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(80), nullable=True)

    session: Mapped["AgentSession"] = relationship()

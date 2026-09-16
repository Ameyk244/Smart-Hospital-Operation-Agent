"""JSON-safe output shapes for the observability trace and conversation
history — the frontend's trace panel and chat panel read these directly.

Why it exists: same reasoning as app/schemas/hospital.py — one schema-
validated shape between the DB layer and the API response, not a hand-built
dict at each route.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db.models.agent import EventStatus, MessageRole


class AgentEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    round_num: int
    event_type: str
    tool_name: str | None
    arguments_json: dict[str, Any] | None
    status: EventStatus
    latency_ms: int | None
    error_category: str | None
    created_at: datetime


class ConversationMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    role: MessageRole
    content: str
    created_at: datetime

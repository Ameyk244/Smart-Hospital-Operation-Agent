"""JSON-safe output shapes for the observability trace and conversation
history — the frontend's trace panel and chat panel read these directly.

Why it exists: same reasoning as app/schemas/hospital.py — one schema-
validated shape between the DB layer and the API response, not a hand-built
dict at each route.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.db.models.agent import AgentEvent, EventStatus, MessageRole


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


class PendingConfirmation(BaseModel):
    """A reschedule the agent proposed and the user has not yet confirmed or
    cancelled (`reschedule_proposed` event, see event_repository)."""

    proposal_id: int
    appointment_code: str
    scanner_code: str
    new_start: str | None

    @classmethod
    def from_event(cls, event: AgentEvent) -> "PendingConfirmation":
        arguments = event.arguments_json
        return cls(
            proposal_id=event.id,
            appointment_code=arguments["appointment_code"],
            scanner_code=arguments["scanner_code"],
            new_start=arguments.get("new_start"),
        )


class PendingBedAction(BaseModel):
    """An assign_bed / release_bed the agent proposed and the user has not yet
    confirmed or cancelled (`bed_action_proposed` event)."""

    proposal_id: int
    action: Literal["assign_bed", "release_bed"]
    bed_code: str
    patient_code: str | None

    @classmethod
    def from_event(cls, event: AgentEvent) -> "PendingBedAction":
        arguments = event.arguments_json
        return cls(
            proposal_id=event.id,
            action=event.tool_name,
            bed_code=arguments["bed_code"],
            patient_code=arguments.get("patient_code"),
        )

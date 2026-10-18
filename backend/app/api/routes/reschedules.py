"""Resolving a reschedule the agent proposed (REQUIRE_RESCHEDULE_CONFIRMATION).

Why it exists: with confirmation required, `reschedule_appointment` only
records a `reschedule_proposed` event. These routes let the user confirm
(run the same `reassign_scanner` command through `CommandRunner`, attributed
to the user) or cancel it. Either way the exchange is added to the chat
history and the agent's checkpoint, so the agent and a page reload both see it.

What calls it: the frontend's confirmation card.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.chat import get_checkpointer, remember_for_agent
from app.db.models.agent import AgentEvent, EventStatus, MessageRole
from app.db.repositories.event_repository import (
    RESCHEDULE_CANCELLED,
    RESCHEDULE_CONFIRMED,
    EventRepository,
)
from app.db.repositories.session_repository import SessionRepository
from app.db.session import get_db, get_session_factory
from app.execution.commands import Command, CommandRunner
from app.observability.tracing import record_event
from app.schemas.observability import PendingConfirmation

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class ConfirmedReschedule(BaseModel):
    status: str = "confirmed"
    message: str
    touched_entity_codes: list[str]


class CancelledReschedule(BaseModel):
    status: str = "cancelled"
    message: str


@router.get("/{session_id}/pending-reschedules", response_model=list[PendingConfirmation])
async def get_pending_reschedules(
    session_id: str, db: AsyncSession = Depends(get_db)
) -> list[PendingConfirmation]:
    proposals = await EventRepository(db).pending_reschedule_proposals(session_id)
    return [PendingConfirmation.from_event(p) for p in proposals]


async def _pending_proposal(db: AsyncSession, session_id: str, proposal_id: int) -> AgentEvent:
    repo = EventRepository(db)
    proposal = await repo.get_reschedule_proposal(session_id, proposal_id)
    if proposal is None:
        raise HTTPException(404, "Unknown reschedule proposal")
    if proposal_id not in {p.id for p in await repo.pending_reschedule_proposals(session_id)}:
        raise HTTPException(409, "This reschedule proposal was already resolved")
    return proposal


async def _resolve(
    db: AsyncSession,
    session_id: str,
    proposal_id: int,
    *,
    event_type: str,
    user_text: str,
    reply: str,
    session_factory,
    checkpointer,
) -> None:
    await record_event(
        db,
        session_id=session_id,
        round_num=0,
        event_type=event_type,
        status=EventStatus.SUCCESS,
        tool_name="reschedule_appointment",
        arguments={"proposal_id": proposal_id},
    )
    messages = SessionRepository(db)
    await messages.append_message(session_id, MessageRole.USER, user_text)
    await messages.append_message(session_id, MessageRole.ASSISTANT, reply)
    await db.commit()
    await remember_for_agent(
        session_id=session_id,
        user_text=user_text,
        assistant_text=reply,
        session_factory=session_factory,
        checkpointer=checkpointer,
    )


@router.post("/{session_id}/reschedule/{proposal_id}/confirm", response_model=ConfirmedReschedule)
async def confirm_reschedule(
    session_id: str,
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    session_factory=Depends(get_session_factory),
    checkpointer=Depends(get_checkpointer),
) -> ConfirmedReschedule:
    proposal = await _pending_proposal(db, session_id, proposal_id)
    result = await CommandRunner(db, actor=f"user_confirmed:{session_id}").execute(
        Command("reassign_scanner", dict(proposal.arguments_json))
    )
    if not result.success:
        raise HTTPException(409, result.error)

    appointment_code = result.data["code"]
    scanner_code = result.data["scanner"]["code"]
    reply = f"Moved appointment {appointment_code} to scanner {scanner_code}."
    await _resolve(
        db,
        session_id,
        proposal_id,
        event_type=RESCHEDULE_CONFIRMED,
        user_text="Confirmed the proposed move.",
        reply=reply,
        session_factory=session_factory,
        checkpointer=checkpointer,
    )
    return ConfirmedReschedule(message=reply, touched_entity_codes=[appointment_code, scanner_code])


@router.post("/{session_id}/reschedule/{proposal_id}/cancel", response_model=CancelledReschedule)
async def cancel_reschedule(
    session_id: str,
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    session_factory=Depends(get_session_factory),
    checkpointer=Depends(get_checkpointer),
) -> CancelledReschedule:
    proposal = await _pending_proposal(db, session_id, proposal_id)
    arguments = proposal.arguments_json
    reply = (
        f"Cancelled; appointment {arguments['appointment_code']} was not moved "
        f"to scanner {arguments['scanner_code']}."
    )
    await _resolve(
        db,
        session_id,
        proposal_id,
        event_type=RESCHEDULE_CANCELLED,
        user_text="Cancelled the proposed move.",
        reply=reply,
        session_factory=session_factory,
        checkpointer=checkpointer,
    )
    return CancelledReschedule(message=reply)

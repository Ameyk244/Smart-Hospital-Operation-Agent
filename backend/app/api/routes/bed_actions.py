"""Resolving a bed change the agent proposed (REQUIRE_BED_CONFIRMATION).

Why it exists: with confirmation required, `assign_bed` / `release_bed` only
record a `bed_action_proposed` event. These routes let the user confirm (run the
same command through `CommandRunner`, attributed to the user) or cancel it.
Same shape as `reschedules.py`; `proposals.py` holds the shared lookup and the
resolution step that records the exchange in the chat history and the agent's
checkpoint.

What calls it: the frontend's confirmation card.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.chat import get_checkpointer
from app.api.routes.proposals import pending_proposal, resolve
from app.db.models.agent import AgentEvent
from app.db.repositories.event_repository import (
    BED_ACTION_CANCELLED,
    BED_ACTION_CONFIRMED,
    EventRepository,
)
from app.db.session import get_db, get_session_factory
from app.execution.commands import Command, CommandRunner
from app.schemas.observability import PendingBedAction

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class ConfirmedBedAction(BaseModel):
    status: str = "confirmed"
    message: str
    touched_entity_codes: list[str]


class CancelledBedAction(BaseModel):
    status: str = "cancelled"
    message: str


def _describe(arguments: dict, tool_name: str, *, done: bool) -> str:
    bed_code = arguments["bed_code"]
    if tool_name == "assign_bed":
        patient_code = arguments["patient_code"]
        if done:
            return f"Assigned patient {patient_code} to bed {bed_code}."
        return f"Cancelled; patient {patient_code} was not assigned to bed {bed_code}."
    if done:
        return f"Released bed {bed_code}."
    return f"Cancelled; bed {bed_code} was not released."


@router.get("/{session_id}/pending-bed-actions", response_model=list[PendingBedAction])
async def get_pending_bed_actions(
    session_id: str, db: AsyncSession = Depends(get_db)
) -> list[PendingBedAction]:
    proposals = await EventRepository(db).pending_bed_proposals(session_id)
    return [PendingBedAction.from_event(p) for p in proposals]


async def _pending_proposal(db: AsyncSession, session_id: str, proposal_id: int) -> AgentEvent:
    return await pending_proposal(
        db,
        session_id,
        proposal_id,
        label="bed action",
        get=EventRepository.get_bed_proposal,
        pending=EventRepository.pending_bed_proposals,
    )


@router.post("/{session_id}/bed-actions/{proposal_id}/confirm", response_model=ConfirmedBedAction)
async def confirm_bed_action(
    session_id: str,
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    session_factory=Depends(get_session_factory),
    checkpointer=Depends(get_checkpointer),
) -> ConfirmedBedAction:
    proposal = await _pending_proposal(db, session_id, proposal_id)
    arguments = dict(proposal.arguments_json)
    result = await CommandRunner(db, actor=f"user_confirmed:{session_id}").execute(
        Command(proposal.tool_name, arguments)
    )
    if not result.success:
        raise HTTPException(409, result.error)

    reply = _describe(arguments, proposal.tool_name, done=True)
    await resolve(
        db,
        session_id,
        proposal_id,
        event_type=BED_ACTION_CONFIRMED,
        tool_name=proposal.tool_name,
        user_text="Confirmed the proposed bed change.",
        reply=reply,
        session_factory=session_factory,
        checkpointer=checkpointer,
    )
    return ConfirmedBedAction(
        message=reply,
        touched_entity_codes=[arguments["bed_code"], *filter(None, [arguments.get("patient_code")])],
    )


@router.post("/{session_id}/bed-actions/{proposal_id}/cancel", response_model=CancelledBedAction)
async def cancel_bed_action(
    session_id: str,
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    session_factory=Depends(get_session_factory),
    checkpointer=Depends(get_checkpointer),
) -> CancelledBedAction:
    proposal = await _pending_proposal(db, session_id, proposal_id)
    reply = _describe(proposal.arguments_json, proposal.tool_name, done=False)
    await resolve(
        db,
        session_id,
        proposal_id,
        event_type=BED_ACTION_CANCELLED,
        tool_name=proposal.tool_name,
        user_text="Cancelled the proposed bed change.",
        reply=reply,
        session_factory=session_factory,
        checkpointer=checkpointer,
    )
    return CancelledBedAction(message=reply)

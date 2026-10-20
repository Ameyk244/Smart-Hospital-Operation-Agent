"""Shared plumbing for resolving an agent proposal the user confirms or cancels.

Why it exists: reschedules and bed changes follow the same flow (look up the
pending proposal, run or skip the command, record the resolution in the event
log, the chat history and the agent's checkpoint). The routers differ only in
which proposal event type and repository methods they use.

What calls it: `reschedules.py` and `bed_actions.py`.
"""

from collections.abc import Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.chat import remember_for_agent
from app.db.models.agent import AgentEvent, EventStatus, MessageRole
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.session_repository import SessionRepository
from app.observability.tracing import record_event

ProposalGetter = Callable[[EventRepository, str, int], Awaitable[AgentEvent | None]]
ProposalLister = Callable[[EventRepository, str], Awaitable[list[AgentEvent]]]


async def pending_proposal(
    db: AsyncSession,
    session_id: str,
    proposal_id: int,
    *,
    label: str,
    get: ProposalGetter,
    pending: ProposalLister,
) -> AgentEvent:
    repo = EventRepository(db)
    proposal = await get(repo, session_id, proposal_id)
    if proposal is None:
        raise HTTPException(404, f"Unknown {label} proposal")
    if proposal_id not in {p.id for p in await pending(repo, session_id)}:
        raise HTTPException(409, f"This {label} proposal was already resolved")
    return proposal


async def resolve(
    db: AsyncSession,
    session_id: str,
    proposal_id: int,
    *,
    event_type: str,
    tool_name: str,
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
        tool_name=tool_name,
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

"""The unified request endpoint — the API-reachable version of the full
diagram in docs/ARCHITECTURE.md §1, both entrances included.

Flow: parser first, always. A match executes through `CommandRunner`
(no LLM touched). No match goes through the eligibility gate
(`app/agent/eligibility.py`); if eligible, the LangGraph agent
(`app/agent/graph.py`) runs, with this session's prior turns as context.
Every branch appends to `ConversationMessage` (concept 35) so the next
request in the same session has continuity regardless of which path
handled it.

What calls it: the frontend's chat UI; `tests/e2e/*`.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.eligibility import check_eligibility
from app.agent.graph import run_agent
from app.agent.history import to_langchain_messages
from app.agent.providers.factory import get_default_chat_model
from app.config import get_settings
from app.db.models.agent import MessageRole
from app.db.repositories.session_repository import SessionRepository
from app.db.session import get_db, get_session_factory
from app.execution.commands import Command, CommandRunner
from app.parser.parser import parse

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    text: str
    # 36 chars: must fit AgentSession.id (sized for a UUID — see
    # app/db/models/agent.py). Rejecting an oversized client-supplied id
    # here, with a clean 422, is better than letting it reach the DB as an
    # unhandled StringDataRightTruncationError.
    session_id: str | None = Field(None, max_length=36)


class ChatResponse(BaseModel):
    session_id: str
    handled_by: Literal["deterministic", "agent", "rejected"]
    message: str
    data: object | None = None
    terminated_reason: str | None = None


def _format_deterministic_message(command: Command, success: bool, error: str | None) -> str:
    if not success:
        return f"Couldn't complete that: {error}"
    return f"OK — ran {command.name}."


@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    session_factory=Depends(get_session_factory),
) -> ChatResponse:
    session_id = request.session_id or str(uuid.uuid4())
    session_repo = SessionRepository(db)
    await session_repo.get_or_create(session_id)

    # Load history *before* appending this turn, so it doesn't duplicate
    # the message we're about to hand the agent as the new HumanMessage.
    prior_rows = await session_repo.get_recent_messages(session_id, limit=20)
    history = to_langchain_messages(prior_rows)

    await session_repo.append_message(session_id, MessageRole.USER, request.text)
    await db.commit()

    outcome = parse(request.text)
    if outcome.matched:
        command = outcome.command
        assert command is not None
        result = await CommandRunner(db).execute(command)
        message = _format_deterministic_message(command, result.success, result.error)
        await session_repo.append_message(session_id, MessageRole.ASSISTANT, message)
        await db.commit()
        return ChatResponse(
            session_id=session_id,
            handled_by="deterministic",
            message=message,
            data=result.data if result.success else None,
        )

    eligibility = check_eligibility(request.text)
    if not eligibility.eligible:
        message = f"Sorry, I can't process that request ({eligibility.reason})."
        await session_repo.append_message(session_id, MessageRole.ASSISTANT, message)
        await db.commit()
        return ChatResponse(session_id=session_id, handled_by="rejected", message=message)

    settings = get_settings()
    chat_model = get_default_chat_model()
    final_state = await run_agent(
        session_id=session_id,
        user_text=request.text,
        chat_model=chat_model,
        session_factory=session_factory,
        settings=settings,
        history=history,
    )
    last_message = final_state["messages"][-1]
    reply_text = (
        last_message.content
        if isinstance(last_message.content, str)
        else str(last_message.content)
    )
    await session_repo.append_message(session_id, MessageRole.ASSISTANT, reply_text)
    await db.commit()
    return ChatResponse(
        session_id=session_id,
        handled_by="agent",
        message=reply_text,
        terminated_reason=final_state["terminated_reason"],
    )

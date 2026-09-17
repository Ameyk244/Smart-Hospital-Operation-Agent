"""The unified request endpoint — the API-reachable version of the full
diagram in docs/ARCHITECTURE.md §1, both entrances included.

Flow: parser first, always. A match executes through `CommandRunner`
(no LLM touched). No match goes through the domain-relevance gate
(`app/agent/domain_gate.py`) and then the eligibility gate
(`app/agent/eligibility.py`); if both pass, the LangGraph agent
(`app/agent/graph.py`) runs, with this session's prior turns as context.
Every branch appends to `ConversationMessage` (concept 35) so the next
request in the same session has continuity regardless of which path
handled it.

What calls it: the frontend's chat UI; `tests/e2e/*`.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.domain_gate import check_domain_gate
from app.agent.eligibility import check_eligibility
from app.agent.entity_codes import extract_entity_codes
from app.agent.graph import run_agent
from app.agent.history import to_langchain_messages
from app.agent.message_text import extract_text_content
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
    # Every entity `code` (appointment/scanner/patient/department) this
    # turn's command or tool results touched — the frontend uses this to
    # briefly highlight the corresponding rows in the Operations panel.
    # Deliberately just codes, not full entities: the panel already has its
    # own data for each row, this only says *which* rows to flash.
    touched_entity_codes: list[str] = []


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _format_deterministic_message(command: Command, success: bool, error: str | None, data: object) -> str:
    """Builds the deterministic path's chat reply from the command's actual
    result, so it's as informative as the agent path's LLM-synthesized
    reply — not a generic acknowledgment a user has to cross-reference
    against the response's `data` field (or a separate UI panel) to
    understand. Kept in the backend, not the frontend, so `/api/chat`'s
    `message` is self-sufficient for any client, not just this project's own
    UI.

    One branch per command name reachable from `app/parser/parser.py`'s
    grammar; the fallback line only exists as a safety net if that grammar
    ever adds a rule without updating this function to match — it should
    never actually fire in normal use, which is exactly why it's a fallback
    and not a KeyError.
    """
    if not success:
        return f"Couldn't complete that: {error}"

    if command.name == "list_departments":
        departments = data or []
        if not departments:
            return "No departments found."
        names = ", ".join(d["name"] for d in departments)
        return f"Found {_plural(len(departments), 'department')}: {names}."

    if command.name == "list_scanners":
        scanners = data or []
        if not scanners:
            return "No scanners matched that filter."
        parts = ", ".join(f"{s['code']} ({s['type']}, {s['status']})" for s in scanners)
        return f"Found {_plural(len(scanners), 'scanner')}: {parts}."

    if command.name == "search_patients":
        patients = data or []
        if not patients:
            return "No patients matched that search."
        names = ", ".join(f"{p['name']} ({p['code']})" for p in patients)
        return f"Found {_plural(len(patients), 'patient')}: {names}."

    if command.name == "show_next_appointment":
        if not data:
            return "No upcoming appointment found."
        a = data
        return (
            f"Next appointment: {a['code']} for {a['patient']['name']} "
            f"({a['appointment_type']}) at {a['scheduled_start']}, status {a['status']}."
        )

    if command.name == "list_delayed_appointments":
        appointments = data or []
        if not appointments:
            return "No delayed appointments found."
        codes = ", ".join(a["code"] for a in appointments)
        return f"Found {_plural(len(appointments), 'delayed appointment')}: {codes}."

    return f"OK — ran {command.name}."  # safety net; see docstring


def get_checkpointer(request: Request):
    """The long-lived LangGraph checkpointer built once at process startup
    (see app/main.py's `lifespan`). A FastAPI dependency, like `get_db` and
    `get_session_factory`, so tests can override it the same way."""
    return request.app.state.checkpointer


@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    session_factory=Depends(get_session_factory),
    checkpointer=Depends(get_checkpointer),
) -> ChatResponse:
    session_id = request.session_id or str(uuid.uuid4())
    session_repo = SessionRepository(db)
    await session_repo.get_or_create(session_id)

    # Load history *before* appending this turn, so it doesn't duplicate
    # the message we're about to hand the agent as the new HumanMessage.
    # Only used as a fallback when no checkpointer is active — see
    # run_agent's docstring-level comment on why the two can't both apply.
    prior_rows = await session_repo.get_recent_messages(session_id, limit=20)
    history = to_langchain_messages(prior_rows)

    await session_repo.append_message(session_id, MessageRole.USER, request.text)
    await db.commit()

    outcome = parse(request.text)
    if outcome.matched:
        command = outcome.command
        assert command is not None
        result = await CommandRunner(db).execute(command)
        message = _format_deterministic_message(command, result.success, result.error, result.data)
        await session_repo.append_message(session_id, MessageRole.ASSISTANT, message)
        await db.commit()
        return ChatResponse(
            session_id=session_id,
            handled_by="deterministic",
            message=message,
            data=result.data if result.success else None,
            touched_entity_codes=extract_entity_codes(result.data) if result.success else [],
        )

    domain_gate = check_domain_gate(request.text)
    if not domain_gate.in_domain:
        message = (
            "I only handle hospital scheduling and operations — try asking "
            "about appointments, scanners, patients, or your preferences."
        )
        await session_repo.append_message(session_id, MessageRole.ASSISTANT, message)
        await db.commit()
        return ChatResponse(session_id=session_id, handled_by="rejected", message=message)

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
        checkpointer=checkpointer,
    )
    last_message = final_state["messages"][-1]
    # NOT `str(last_message.content)`: when a model turn includes a
    # `thinking` block alongside its `text` block, `.content` is a list of
    # block dicts, not a string — str()-ing that verbatim used to leak the
    # raw block structure (including thinking content and signatures) into
    # the reply the user actually reads. extract_text_content pulls out
    # only the text; see that module's docstring for why the fix belongs
    # here and not in the graph state itself.
    reply_text = extract_text_content(last_message.content)
    await session_repo.append_message(session_id, MessageRole.ASSISTANT, reply_text)
    await db.commit()
    return ChatResponse(
        session_id=session_id,
        handled_by="agent",
        message=reply_text,
        terminated_reason=final_state["terminated_reason"],
        touched_entity_codes=final_state.get("touched_entity_codes", []),
    )

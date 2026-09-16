"""Session-scoped read endpoints: the observability trace (concept 48's
UI-facing half) and conversation history — what the frontend's trace panel
and chat panel actually poll.

Why it exists: docs/ARCHITECTURE.md §9 calls for a UI trace panel showing
the action-level events emitted during agent processing. This is a
deliberately simple polling endpoint rather than a streaming (SSE/
WebSocket) one — the frontend re-fetches after each chat turn completes
(and may poll on a short interval while a request is in flight). A
streaming trace would need the tool_node's `record_event` calls to also
push into a live pub/sub channel, which is meaningfully more
infrastructure for a learning project whose complexity budget is meant to
go to the agent architecture, not a real-time transport layer.

What calls it: the frontend's trace panel (`GET .../trace`) and chat
history restore-on-load (`GET .../messages`).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.event_repository import EventRepository
from app.db.repositories.session_repository import SessionRepository
from app.db.session import get_db
from app.schemas.observability import AgentEventOut, ConversationMessageOut

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("/{session_id}/trace", response_model=list[AgentEventOut])
async def get_trace(session_id: str, db: AsyncSession = Depends(get_db)) -> list[AgentEventOut]:
    events = await EventRepository(db).list_for_session(session_id)
    return [AgentEventOut.model_validate(e) for e in events]


@router.get("/{session_id}/messages", response_model=list[ConversationMessageOut])
async def get_messages(
    session_id: str, db: AsyncSession = Depends(get_db)
) -> list[ConversationMessageOut]:
    messages = await SessionRepository(db).get_recent_messages(session_id, limit=100)
    return [ConversationMessageOut.model_validate(m) for m in messages]

"""Session identity + short-term conversation state (concepts 34, 35).

Why it exists: `AgentSession` and `ConversationMessage` are the durable
record a session's transcript, kept deliberately separate from LangGraph's
own checkpoint tables (owned by `langgraph-checkpoint-postgres`) and from
`Preference` (persistent, cross-session-style memory) — see
docs/ARCHITECTURE.md §7 for why these three are never merged.

What calls it: `app/api/routes/chat.py` (get-or-create a session, append
turns), `app/agent/graph.py` (loading recent history to seed `AgentState`).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.agent import AgentSession, ConversationMessage, MessageRole


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(self, session_id: str) -> AgentSession:
        result = await self._session.execute(
            select(AgentSession).where(AgentSession.id == session_id)
        )
        agent_session = result.scalar_one_or_none()
        if agent_session is None:
            agent_session = AgentSession(id=session_id)
            self._session.add(agent_session)
            await self._session.flush()
        return agent_session

    async def append_message(
        self, session_id: str, role: MessageRole, content: str
    ) -> ConversationMessage:
        message = ConversationMessage(session_id=session_id, role=role, content=content)
        self._session.add(message)
        await self._session.flush()
        return message

    async def get_recent_messages(
        self, session_id: str, limit: int = 20
    ) -> list[ConversationMessage]:
        # Tiebreak on id, not just created_at: two messages appended in the
        # same request can land in the same millisecond, and created_at alone
        # would make their relative order (and thus conversation history)
        # nondeterministic.
        stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.session_id == session_id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(reversed(result.scalars().all()))

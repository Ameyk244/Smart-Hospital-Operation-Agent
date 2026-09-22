"""Data access for the observability trace (concepts 48-50).

Why it exists: raw persistence for `AgentEvent`. `app/observability/tracing.py`
is the only caller — it decides *what* counts as an event and *when* one is
emitted; this module only writes/reads the rows.

What calls it: `app/observability/tracing.py`.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.agent import AgentEvent, EventStatus


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        session_id: str,
        round_num: int,
        event_type: str,
        status: EventStatus,
        tool_name: str | None = None,
        arguments: dict | None = None,
        latency_ms: int | None = None,
        error_category: str | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            session_id=session_id,
            round_num=round_num,
            event_type=event_type,
            tool_name=tool_name,
            arguments_json=arguments,
            status=status,
            latency_ms=latency_ms,
            error_category=error_category,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_for_session(self, session_id: str) -> list[AgentEvent]:
        # See session_repository.get_recent_messages for why id is a
        # necessary tiebreaker alongside created_at.
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.session_id == session_id)
            .order_by(AgentEvent.created_at, AgentEvent.id)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_event_type(
        self, event_type: str, *, session_id: str | None = None
    ) -> list[AgentEvent]:
        """Every event of one type, optionally scoped to a single session.

        Exists so callers that aggregate across sessions (the Jev cost
        comparison in `app/api/routes/cost.py`) still go through this
        repository instead of writing raw SQL in a route — the same reason
        `list_for_session` exists. Ordered identically to `list_for_session`
        so callers can rely on one stable ordering rule.
        """
        stmt = select(AgentEvent).where(AgentEvent.event_type == event_type)
        if session_id is not None:
            stmt = stmt.where(AgentEvent.session_id == session_id)
        stmt = stmt.order_by(AgentEvent.created_at, AgentEvent.id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

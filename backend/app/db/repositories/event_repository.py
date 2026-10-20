"""Data access for the observability trace (concepts 48-50).

Why it exists: raw persistence for `AgentEvent`. `app/observability/tracing.py`
is the only caller — it decides *what* counts as an event and *when* one is
emitted; this module only writes/reads the rows.

What calls it: `app/observability/tracing.py`.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.agent import AgentEvent, EventStatus


RESCHEDULE_PROPOSED = "reschedule_proposed"
RESCHEDULE_CONFIRMED = "reschedule_confirmed"
RESCHEDULE_CANCELLED = "reschedule_cancelled"

BED_ACTION_PROPOSED = "bed_action_proposed"
BED_ACTION_CONFIRMED = "bed_action_confirmed"
BED_ACTION_CANCELLED = "bed_action_cancelled"


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

    async def _get_proposal(
        self, session_id: str, proposal_id: int, proposed: str
    ) -> AgentEvent | None:
        proposal = await self._session.get(AgentEvent, proposal_id)
        if proposal is None or proposal.session_id != session_id:
            return None
        return proposal if proposal.event_type == proposed else None

    async def _pending_proposals(
        self, session_id: str, proposed: str, confirmed: str, cancelled: str
    ) -> list[AgentEvent]:
        """Proposals in this session not yet confirmed or cancelled, oldest
        first. Resolution rows point back at their proposal by `proposal_id`."""
        stmt = (
            select(AgentEvent)
            .where(
                AgentEvent.session_id == session_id,
                AgentEvent.event_type.in_([proposed, confirmed, cancelled]),
            )
            .order_by(AgentEvent.created_at, AgentEvent.id)
        )
        events = (await self._session.execute(stmt)).scalars().all()
        resolved = {e.arguments_json["proposal_id"] for e in events if e.event_type != proposed}
        return [e for e in events if e.event_type == proposed and e.id not in resolved]

    async def get_reschedule_proposal(self, session_id: str, proposal_id: int) -> AgentEvent | None:
        return await self._get_proposal(session_id, proposal_id, RESCHEDULE_PROPOSED)

    async def pending_reschedule_proposals(self, session_id: str) -> list[AgentEvent]:
        return await self._pending_proposals(
            session_id, RESCHEDULE_PROPOSED, RESCHEDULE_CONFIRMED, RESCHEDULE_CANCELLED
        )

    async def get_bed_proposal(self, session_id: str, proposal_id: int) -> AgentEvent | None:
        return await self._get_proposal(session_id, proposal_id, BED_ACTION_PROPOSED)

    async def pending_bed_proposals(self, session_id: str) -> list[AgentEvent]:
        return await self._pending_proposals(
            session_id, BED_ACTION_PROPOSED, BED_ACTION_CONFIRMED, BED_ACTION_CANCELLED
        )

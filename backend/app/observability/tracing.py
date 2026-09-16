"""The action-level agent trace (concepts 48, 50).

Why it exists: docs/ARCHITECTURE.md §9 requires an execution trace I can
read after the fact — parser outcome, gate decision, each tool call's
validation/grounding/execution outcome, round/limit terminations. This
module is the single place that trace is written, as both a DB row
(`AgentEvent`, queryable later for the UI's trace panel) and a structured
log line (for live tailing). It deliberately has no field for model
reasoning/chain-of-thought — only what was requested, decided, and the
outcome, per the master prompt's explicit prohibition on capturing private
reasoning.

What calls it: `app/agent/graph.py`'s agent_node and tool_node, once per
decision point.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.agent import EventStatus
from app.db.repositories.event_repository import EventRepository
from app.observability.logging_config import get_logger

_logger = get_logger("agent.trace")


async def record_event(
    session: AsyncSession,
    *,
    session_id: str,
    round_num: int,
    event_type: str,
    status: EventStatus,
    tool_name: str | None = None,
    arguments: dict[str, Any] | None = None,
    latency_ms: int | None = None,
    error_category: str | None = None,
) -> None:
    await EventRepository(session).record(
        session_id=session_id,
        round_num=round_num,
        event_type=event_type,
        status=status,
        tool_name=tool_name,
        arguments=arguments,
        latency_ms=latency_ms,
        error_category=error_category,
    )
    _logger.info(
        event_type,
        session_id=session_id,
        round=round_num,
        status=status.value,
        tool_name=tool_name,
        latency_ms=latency_ms,
        error_category=error_category,
    )

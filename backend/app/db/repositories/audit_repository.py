"""Data access for the mutation audit log (observability Phase 6).

Why it exists: raw persistence for `AuditRecord`. It deliberately has no
update or delete method: the table is append-only, and the database
enforces it too (see `app/db/models/audit.py`).

Correlation ids are read from the current context rather than passed in:
`request_id` from the structlog contextvars bound by
`app/observability/request_context.py`, and `trace_id` from the active
OpenTelemetry span. Both are best-effort (None outside a request or with
tracing off). The actor and the before/after are always explicit.

What calls it: `app/execution/commands/appointment_commands.py`.
"""

from typing import Any

import structlog
from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditRecord


def _current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_code: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> AuditRecord:
        request_id = structlog.contextvars.get_contextvars().get("request_id")
        record = AuditRecord(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_code=entity_code,
            before_json=before,
            after_json=after,
            request_id=request_id if isinstance(request_id, str) else None,
            trace_id=_current_trace_id(),
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def list_for_entity(self, entity_type: str, entity_code: str) -> list[AuditRecord]:
        result = await self._session.execute(
            select(AuditRecord)
            .where(AuditRecord.entity_type == entity_type, AuditRecord.entity_code == entity_code)
            .order_by(AuditRecord.id)
        )
        return list(result.scalars().all())

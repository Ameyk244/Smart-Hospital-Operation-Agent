"""One agent turn's tool calls, without the model or the graph.

Why it exists: everything that happens to a single tool call after the model
asked for it -- budget check, unknown-tool and argument rejection, grounding,
timeout, events, metrics and the `tool.execute` span -- lives here, so the
LangGraph `tool_node` (app/agent/graph.py) is only a loop over it and any
other caller gets exactly the same bounds.

What calls it: `tool_node` in `app/agent/graph.py`.

Fails: never raises for a bad call -- rejections, timeouts and tool errors
come back as the tool message content so the model can react, and the
counters and `terminated_reason` are updated in place. A genuine bug (e.g. a
DB connection error) propagates.
"""

import asyncio
import json
import time

from opentelemetry import trace
from opentelemetry.context import Context
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.entity_codes import extract_entity_codes
from app.agent.grounding import GroundingRejectedError
from app.agent.tools import TOOL_REGISTRY, ToolExecutionError
from app.agent.tools.base import ToolSpec
from app.config import Settings
from app.db.models.agent import EventStatus
from app.observability import metrics, spans
from app.observability.tracing import record_event


def _tool_outcome(
    tool_span: trace.Span,
    tool_name: str,
    status: str,
    error_category: str | None = None,
    seconds: float | None = None,
) -> None:
    """One tool call's outcome, to both the metric and its span. Failures
    (timeouts, tool errors) mark the span ERROR; rejections (grounding,
    invalid or unknown calls, exhausted budget) are the system working as
    designed and stay unset, findable by `tool.error_category`."""
    metrics.record_tool_call(tool_name, status, error_category, seconds)
    spans.set_attributes(
        tool_span,
        **{"tool.status": status.lower(), "tool.error_category": error_category},
    )
    if status == EventStatus.FAILURE.value:
        spans.mark_error(tool_span, error_category or "tool_error")


class ToolTurn:
    def __init__(
        self,
        *,
        session_id: str,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        tool_call_count: int = 0,
        invalid_call_count: int = 0,
        touched_entity_codes: list[str] | None = None,
    ) -> None:
        self._session_id = session_id
        self._session_factory = session_factory
        self._settings = settings
        self.tool_call_count = tool_call_count
        self.invalid_call_count = invalid_call_count
        self.terminated_reason: str | None = None
        # Carried forward across rounds *within this turn* (tool_node can
        # run several times before the loop ends) but reset to [] fresh at
        # the start of every new run_agent() call — see state.py's comment
        # on why this deliberately isn't a reducer-merged field.
        self.touched_entity_codes = list(touched_entity_codes or [])
        self._touched_seen = set(self.touched_entity_codes)

    async def call(
        self,
        *,
        name: str,
        args: dict | None,
        round_num: int,
        parent_context: Context | None = None,
    ) -> str:
        """Runs one tool call and returns the tool message content. `args`
        is None when the model's arguments could not be parsed."""
        # Registered name or "unknown": a model-invented name is free
        # text and never goes on a span or a metric.
        tool_label = name if name in TOOL_REGISTRY else metrics.UNKNOWN_TOOL
        with spans.span(
            "tool.execute", {"tool.name": tool_label}, context=parent_context
        ) as tool_span:
            if (
                self.terminated_reason is not None
                or self.tool_call_count >= self._settings.max_tool_calls
            ):
                self.terminated_reason = self.terminated_reason or "max_tool_calls_exceeded"
                _tool_outcome(tool_span, tool_label, EventStatus.REJECTED.value, "budget_exhausted")
                # budget exhaustion isn't an invalid-call condition
                return "Not executed: this session's tool-call budget is exhausted."

            # Every branch of _execute returns to here rather than ending
            # the call early, specifically so the invalid-call-limit check
            # always runs exactly once per call regardless of which branch
            # handled it — an earlier version skipped that check for the
            # unknown-tool/invalid-argument branches, letting a model that
            # only ever requested unknown tools loop all the way to
            # max_agent_rounds instead of stopping at max_invalid_tool_calls.
            content = await self._execute(tool_span, name, args, round_num)
            if self.invalid_call_count >= self._settings.max_invalid_tool_calls:
                self.terminated_reason = "too_many_invalid_calls"
            return content

    async def _execute(
        self, tool_span: trace.Span, name: str, args: dict | None, round_num: int
    ) -> str:
        spec = TOOL_REGISTRY.get(name)
        if spec is None:
            self.invalid_call_count += 1
            _tool_outcome(tool_span, metrics.UNKNOWN_TOOL, EventStatus.REJECTED.value, "unknown_tool")
            await self._record_rejection(round_num, name, None, "unknown_tool")
            return f"Unknown tool {name!r}"

        validated_args, error = self._validate(spec, args)
        if validated_args is None:
            self.invalid_call_count += 1
            _tool_outcome(tool_span, spec.name, EventStatus.REJECTED.value, "invalid_argument")
            await self._record_rejection(round_num, spec.name, args, "invalid_argument")
            return error

        start = time.monotonic()
        async with self._session_factory() as session:
            try:
                # Outcome is recorded by _tool_outcome below, not by the span.
                result = await asyncio.wait_for(
                    spec.handler(session, self._session_id, validated_args),
                    timeout=self._settings.tool_timeout_seconds,
                )
            except TimeoutError:
                await session.rollback()
                self.tool_call_count += 1
                latency_ms = int((time.monotonic() - start) * 1000)
                _tool_outcome(
                    tool_span, spec.name, EventStatus.FAILURE.value, "timeout", time.monotonic() - start
                )
                await self._record_executed(
                    session, round_num, spec, args, EventStatus.FAILURE, latency_ms, "timeout"
                )
                return "Tool call timed out"
            except GroundingRejectedError as exc:
                await session.rollback()
                self.tool_call_count += 1
                self.invalid_call_count += 1
                latency_ms = int((time.monotonic() - start) * 1000)
                _tool_outcome(
                    tool_span,
                    spec.name,
                    EventStatus.REJECTED.value,
                    "grounding_rejected",
                    time.monotonic() - start,
                )
                await self._record_executed(
                    session, round_num, spec, args, EventStatus.REJECTED, latency_ms, "grounding_rejected"
                )
                return str(exc)
            except ToolExecutionError as exc:
                await session.rollback()
                self.tool_call_count += 1
                latency_ms = int((time.monotonic() - start) * 1000)
                _tool_outcome(
                    tool_span, spec.name, EventStatus.FAILURE.value, exc.category, time.monotonic() - start
                )
                await self._record_executed(
                    session, round_num, spec, args, EventStatus.FAILURE, latency_ms, exc.category
                )
                return f"Error: {exc}"

            await session.commit()
            self.tool_call_count += 1
            latency_ms = int((time.monotonic() - start) * 1000)
            _tool_outcome(tool_span, spec.name, EventStatus.SUCCESS.value, None, time.monotonic() - start)
            await self._record_executed(
                session, round_num, spec, args, EventStatus.SUCCESS, latency_ms, None
            )
            for code in extract_entity_codes(result):
                if code not in self._touched_seen:
                    self._touched_seen.add(code)
                    self.touched_entity_codes.append(code)
            return json.dumps(result, default=str)

    @staticmethod
    def _validate(spec: ToolSpec, args: dict | None) -> tuple[BaseModel | None, str]:
        if args is None:
            return None, "Invalid arguments: could not parse JSON"
        try:
            return spec.args_schema.model_validate(args), ""
        except ValidationError as exc:
            return None, f"Invalid arguments: {exc}"

    async def _record_rejection(
        self, round_num: int, tool_name: str, args: dict | None, error_category: str
    ) -> None:
        async with self._session_factory() as session:
            await record_event(
                session,
                session_id=self._session_id,
                round_num=round_num,
                event_type="tool_requested",
                status=EventStatus.REJECTED,
                tool_name=tool_name,
                arguments=args,
                error_category=error_category,
            )
            await session.commit()

    async def _record_executed(
        self,
        session: AsyncSession,
        round_num: int,
        spec: ToolSpec,
        args: dict | None,
        status: EventStatus,
        latency_ms: int,
        error_category: str | None,
    ) -> None:
        await record_event(
            session,
            session_id=self._session_id,
            round_num=round_num,
            event_type="tool_executed",
            status=status,
            tool_name=spec.name,
            arguments=args,
            latency_ms=latency_ms,
            error_category=error_category,
        )
        await session.commit()

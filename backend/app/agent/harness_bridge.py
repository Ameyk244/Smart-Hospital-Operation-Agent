"""Runs one agent turn through the harness instead of the in-process graph.

Why it exists: with `AGENT_RUNTIME=harness` the model loop lives in a separate
process (contract: docs/harness-smart-hospital-operations.md sections 5-7).
This module is the backend's side of it: it sends the turn, serves the tool
calls the harness makes back through the same `ToolTurn` the graph uses, maps
the finish payload to the `AgentState` shape `run_agent` returns, and writes
the turn into the checkpoint so either runtime can continue the thread.

What calls it: `handle_chat_message()` in `app/api/routes/chat.py`, at the
agent step only -- after the parser, domain gate, eligibility gate and Jev.

Fails: never raises for a harness problem. A non-200, transport error, timeout
or malformed body becomes `harness_unavailable`; harness and provider error
text is never shown or logged, only the stop reason and the text's length.
There is no fallback to the in-process runtime. A genuine bug (e.g. a DB
connection error) propagates.
"""

import asyncio
import json
import uuid
from typing import Any, Literal

import httpx
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from opentelemetry import trace
from opentelemetry.context import Context
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import harness_registry
from app.agent.graph import (
    build_system_message,
    get_thread_messages,
    record_turn_messages,
)
from app.agent.history import to_openai_messages
from app.agent.state import AgentState
from app.agent.tools import all_tool_dicts
from app.agent.turn import ToolTurn
from app.config import Settings
from app.db.models.agent import EventStatus
from app.db.repositories.session_repository import SessionRepository
from app.observability import metrics, spans
from app.observability.logging_config import get_logger
from app.observability.tracing import record_event

logger = get_logger("agent.harness_bridge")

# Added to the turn deadline so the harness reports its own time_limit before
# the HTTP client gives up: its margin, a 20 s watchdog (longer than one tool
# call may run) and two 5 s bounded cleanups all fit inside it.
HTTP_GRACE_SECONDS = 40

NOT_EXECUTED = "Not executed: the turn ended before this call ran."

_STOP_REASONS = {
    "round_limit": "max_rounds_exceeded",
    "time_limit": "llm_timeout",
    "error": "harness_error",
}

_REPLIES = {
    "max_tool_calls_exceeded": "I stopped because this session's tool-call limit was reached.",
    "too_many_invalid_calls": "I stopped because too many tool calls were invalid.",
    "max_rounds_exceeded": "I stopped because the request needed too many steps.",
    "llm_timeout": "The model took too long to respond. Please try again.",
    "harness_error": "Something went wrong while processing that request. Please try again.",
    "harness_unavailable": "The assistant is temporarily unavailable. Please try again shortly.",
}

# Terminal events the graph also writes (backend end_turn reasons write none).
_TERMINAL_EVENTS = {
    "max_rounds_exceeded": (EventStatus.REJECTED, None),
    "llm_timeout": (EventStatus.FAILURE, "timeout"),
    "harness_error": (EventStatus.FAILURE, "harness_error"),
    "harness_unavailable": (EventStatus.FAILURE, "harness_unavailable"),
}


class HarnessCall(BaseModel):
    id: str
    name: str
    arguments: str
    result: str | None = None


class HarnessUsage(BaseModel):
    prompt_tokens: int = Field(0, ge=0)
    completion_tokens: int = Field(0, ge=0)
    cached_tokens: int = Field(0, ge=0)


class HarnessRound(BaseModel):
    round: int
    text: str = ""
    tool_calls: list[HarnessCall] = []
    usage: HarnessUsage = HarnessUsage()


class HarnessFinish(BaseModel):
    stop_reason: Literal["round_limit", "time_limit", "error"] | None = None
    error: str | None = None
    model_text: str = ""
    rounds: int = 0
    assistant_rounds: list[HarnessRound] = []


class _RecordingTurn:
    """What the registry holds: a `ToolTurn` that also remembers what it
    served, because the rebuild needs each call's content by (round, call_id).

    Calls run one at a time, as in the graph, so the caps cannot be overshot by
    parallel requests. A repeated (round, call_id) returns the stored content
    instead of running the tool again, which matters for the one write tool.
    Its `context` parents the `tool.execute` spans on the `harness.turn` span."""

    def __init__(self, turn: ToolTurn, context: Context) -> None:
        self.turn = turn
        self.served: list[tuple[int, str, str, str]] = []
        self._context = context
        self._lock = asyncio.Lock()

    @property
    def terminated_reason(self) -> str | None:
        return self.turn.terminated_reason

    async def call(self, *, call_id: str, name: str, args: dict | None, round_num: int) -> str:
        async with self._lock:
            for served_round, served_id, _, content in self.served:
                if (served_round, served_id) == (round_num, call_id):
                    return content
            content = await self.turn.call(
                name=name, args=args, round_num=round_num, parent_context=self._context
            )
            self.served.append((round_num, call_id, name, content))
            return content

    async def drain(self) -> None:
        """Waits for a call already running, so `served` is final."""
        async with self._lock:
            pass


def resolve_outcome(backend_reason: str | None, finish: HarnessFinish | None) -> tuple[str | None, str]:
    """The turn's `terminated_reason` and reply text. A backend `end_turn`
    wins over any harness stop reason; `finish` is None when the harness was
    unreachable. The model's text is the reply only on a clean completion."""
    if backend_reason:
        reason = backend_reason
    elif finish is None:
        reason = "harness_unavailable"
    else:
        reason = _STOP_REASONS.get(finish.stop_reason or "")
    if reason is None:
        assert finish is not None
        if not finish.model_text.strip():
            return "harness_error", _REPLIES["harness_error"]
        return None, finish.model_text
    return reason, _REPLIES[reason]


def _parse_args(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def rebuild_turn_messages(
    assistant_rounds: list[HarnessRound],
    served: list[tuple[int, str, str, str]],
    reply: str,
) -> list[BaseMessage]:
    """The turn as LangChain messages. Provider call ids repeat across
    rounds, so served content is matched by (round, call_id). A call the
    gateway ran itself carries its own `result`, used when the backend did not
    serve it. A reported call with neither gets a closer; a served call no round mentions ran, so it
    comes first, as a call of its own. The result always ends with an
    assistant message and never leaves a tool call without its tool message."""
    content_by_call = {(round_num, call_id): content for round_num, call_id, _, content in served}
    reported = {(r.round, c.id) for r in assistant_rounds for c in r.tool_calls}
    messages: list[BaseMessage] = []
    for round_num, call_id, name, content in served:
        if (round_num, call_id) not in reported:
            messages.append(
                AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": {}}])
            )
            messages.append(ToolMessage(content=content, tool_call_id=call_id))
    for harness_round in assistant_rounds:
        calls = [
            {"id": c.id, "name": c.name, "args": _parse_args(c.arguments)}
            for c in harness_round.tool_calls
        ]
        messages.append(AIMessage(content=harness_round.text, tool_calls=calls))
        for call in harness_round.tool_calls:
            fallback = NOT_EXECUTED if call.result is None else call.result
            content = content_by_call.get((harness_round.round, call.id), fallback)
            messages.append(ToolMessage(content=content, tool_call_id=call.id))
    last = messages[-1] if messages else None
    if not (isinstance(last, AIMessage) and not last.tool_calls and last.content == reply):
        messages.append(AIMessage(content=reply))
    return messages


async def _post_turn(settings: Settings, payload: dict) -> dict:
    async with httpx.AsyncClient(
        timeout=settings.harness_turn_timeout_seconds + HTTP_GRACE_SECONDS
    ) as client:
        response = await client.post(
            f"{settings.harness_url}/v1/turns",
            json=payload,
            headers={"x-harness-token": settings.harness_token or ""},
        )
    response.raise_for_status()
    return response.json()


async def _request_finish(settings: Settings, payload: dict) -> HarnessFinish:
    return HarnessFinish.model_validate(await _post_turn(settings, payload))


async def _record_model_fallback(
    session_factory: async_sessionmaker[AsyncSession], session_id: str, model: str
) -> None:
    async with session_factory() as session:
        await record_event(
            session,
            session_id=session_id,
            round_num=1,
            event_type="model_fallback",
            status=EventStatus.SUCCESS,
            arguments={"model": model},
        )
        await session.commit()


async def _record_terminal_event(
    session_factory: async_sessionmaker[AsyncSession], session_id: str, rounds: int, reason: str
) -> None:
    status, error_category = _TERMINAL_EVENTS[reason]
    async with session_factory() as session:
        await record_event(
            session,
            session_id=session_id,
            round_num=rounds,
            event_type=reason,
            status=status,
            error_category=error_category,
        )
        await session.commit()


async def _record_llm_responses(
    session_factory: async_sessionmaker[AsyncSession], session_id: str, rounds: list[HarnessRound]
) -> None:
    """One `llm_response` row per reported round, shaped like the graph's."""
    async with session_factory() as session:
        for harness_round in rounds:
            calls = len(harness_round.tool_calls)
            await record_event(
                session,
                session_id=session_id,
                round_num=harness_round.round,
                event_type="llm_response",
                status=EventStatus.SUCCESS,
                arguments={
                    **({"tool_call_count": calls} if calls else {}),
                    "input_tokens": harness_round.usage.prompt_tokens,
                    "output_tokens": harness_round.usage.completion_tokens,
                },
            )
        await session.commit()


async def run_agent_via_harness(
    *,
    session_id: str,
    user_text: str,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    history: list[Any],
    checkpointer: Any | None,
) -> AgentState:
    async with session_factory() as session:
        await SessionRepository(session).get_or_create(session_id)
        await session.commit()

    system_message = await build_system_message(
        session_factory, session_id, settings, user_text if settings.agent_skills_enabled else None
    )
    prior = (
        await get_thread_messages(
            session_id=session_id,
            session_factory=session_factory,
            settings=settings,
            checkpointer=checkpointer,
        )
        if checkpointer is not None
        else history
    )
    turn_id = uuid.uuid4().hex
    payload = {
        "turn_id": turn_id,
        "system": system_message.content,
        "history": to_openai_messages([m for m in prior if not isinstance(m, SystemMessage)]),
        "user": user_text,
        "tools": all_tool_dicts(settings),
        "llm": {
            "model": settings.harness_model,
            "max_tokens": settings.harness_max_tokens,
            "deadline_in_ms": settings.harness_turn_timeout_seconds * 1000,
            "max_rounds": settings.max_agent_rounds,
        },
    }
    async with session_factory() as session:
        await record_event(
            session,
            session_id=session_id,
            round_num=1,
            event_type="agent_invoked",
            status=EventStatus.SUCCESS,
        )
        await session.commit()

    finish: HarnessFinish | None = None
    with spans.span("harness.turn", {"harness.model": settings.harness_model}) as turn_span:
        recorder = _RecordingTurn(
            ToolTurn(session_id=session_id, session_factory=session_factory, settings=settings),
            trace.set_span_in_context(turn_span),
        )
        harness_registry.register(turn_id, recorder)
        try:
            finish = await _request_finish(settings, payload)
            if settings.harness_fallback_model and finish.error == "model_error" and finish.stop_reason == "error":
                await recorder.drain()
                if not recorder.served:
                    await _record_model_fallback(session_factory, session_id, settings.harness_fallback_model)
                    llm = {**payload["llm"], "model": settings.harness_fallback_model}
                    finish = await _request_finish(settings, {**payload, "llm": llm})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("harness_unreachable", error_type=type(exc).__name__)
            spans.mark_error(turn_span, "harness_unavailable")
        finally:
            harness_registry.unregister(turn_id)
            await recorder.drain()
        if finish is not None and finish.stop_reason == "error":
            logger.warning("harness_error", stop_reason="error", error_length=len(finish.error or ""))
        rounds = finish.rounds if finish else 0
        spans.set_attributes(
            turn_span,
            **{"harness.rounds": rounds, "harness.stop_reason": finish.stop_reason if finish else None},
        )

    if finish is not None:
        await _record_llm_responses(session_factory, session_id, finish.assistant_rounds)
    reason, reply = resolve_outcome(recorder.terminated_reason, finish)
    if reason in _TERMINAL_EVENTS:
        await _record_terminal_event(session_factory, session_id, rounds, reason)
    metrics.record_agent_turn(rounds, reason)

    turn_messages = [
        HumanMessage(content=user_text),
        *rebuild_turn_messages(finish.assistant_rounds if finish else [], recorder.served, reply),
    ]
    await record_turn_messages(
        session_id=session_id,
        messages=turn_messages,
        session_factory=session_factory,
        settings=settings,
        checkpointer=checkpointer,
    )
    return {
        "session_id": session_id,
        "messages": turn_messages,
        "round_count": rounds,
        "tool_call_count": recorder.turn.tool_call_count,
        "invalid_call_count": recorder.turn.invalid_call_count,
        "write_call_count": recorder.turn.write_call_count,
        "terminated_reason": reason,
        "touched_entity_codes": recorder.turn.touched_entity_codes,
    }

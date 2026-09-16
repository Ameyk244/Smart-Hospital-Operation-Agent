"""The LangGraph agent loop (concepts 10, 11, 12, 27, 28, 29).

Why it exists: this is the orchestration layer docs/ARCHITECTURE.md §5
describes — two nodes, explicit conditional routing, typed state, and every
bound (rounds, tool calls, timeouts, invalid calls) enforced in code you can
read here, not hidden behind a prebuilt `create_agent()`-style call.

Shape:

    agent_node --tool_calls present--> tool_node --> agent_node (loop)
       |no tool calls / bounds hit                 |bounds hit
       v                                           v
      END                                         END

Each tool call gets its own fresh DB session (`session_factory`), not the
node's own — nodes themselves never touch the database directly, only
through a tool handler. This keeps a node's job to "call the model" or
"dispatch tool calls" and nothing else.

What calls it: `app/api/routes/chat.py` (the agent-eligible request path).

Fails: never raises past `run_agent` for a normal bad request — model
timeouts, tool timeouts, invalid tool calls, and grounding rejections are
all caught and folded into `AgentState` (`terminated_reason`, or a
`ToolMessage` describing the failure so the model itself can react). A
genuine bug (e.g. a DB connection error) is allowed to propagate — bounded
execution is about model/tool misbehavior, not masking infrastructure
failures.
"""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.grounding import GroundingRejectedError
from app.agent.state import AgentState
from app.agent.tools import TOOL_REGISTRY, ToolExecutionError, all_tool_dicts
from app.config import Settings
from app.db.models.agent import EventStatus
from app.db.repositories.session_repository import SessionRepository
from app.observability.tracing import record_event

SYSTEM_PROMPT = (
    "You are the Smart Hospital Operations Agent, assisting hospital staff with "
    "scheduling and scanner assignment questions over synthetic data. You have "
    "tools to search appointments and reschedule them to a different scanner. "
    "Always search before acting: you may only pass appointment_code/scanner_code "
    "values into reschedule_appointment that came from a search result earlier in "
    "this conversation — a value you make up will be rejected. If a tool call is "
    "rejected or fails, explain why to the user rather than retrying the same "
    "invented value. Be concise."
)


def _make_agent_node(
    chat_model: BaseChatModel, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> Callable[[AgentState], Any]:
    model_with_tools = chat_model.bind_tools(all_tool_dicts())

    async def agent_node(state: AgentState) -> dict:
        if state["round_count"] >= settings.max_agent_rounds:
            async with session_factory() as session:
                await record_event(
                    session,
                    session_id=state["session_id"],
                    round_num=state["round_count"],
                    event_type="max_rounds_exceeded",
                    status=EventStatus.REJECTED,
                )
                await session.commit()
            return {"terminated_reason": "max_rounds_exceeded"}

        messages = state["messages"]
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=SYSTEM_PROMPT), *messages]

        round_num = state["round_count"] + 1
        async with session_factory() as session:
            await record_event(
                session,
                session_id=state["session_id"],
                round_num=round_num,
                event_type="agent_invoked",
                status=EventStatus.SUCCESS,
            )
            await session.commit()

        try:
            response = await asyncio.wait_for(
                model_with_tools.ainvoke(messages), timeout=settings.llm_timeout_seconds
            )
        except TimeoutError:
            async with session_factory() as session:
                await record_event(
                    session,
                    session_id=state["session_id"],
                    round_num=round_num,
                    event_type="llm_timeout",
                    status=EventStatus.FAILURE,
                    error_category="timeout",
                )
                await session.commit()
            return {
                "round_count": round_num,
                "messages": [AIMessage(content="(the model timed out)")],
                "terminated_reason": "llm_timeout",
            }

        async with session_factory() as session:
            await record_event(
                session,
                session_id=state["session_id"],
                round_num=round_num,
                event_type="llm_response",
                status=EventStatus.SUCCESS,
                arguments={"tool_call_count": len(response.tool_calls)}
                if response.tool_calls
                else None,
            )
            await session.commit()

        return {"round_count": round_num, "messages": [response]}

    return agent_node


def _route_after_agent(state: AgentState) -> str:
    if state.get("terminated_reason"):
        return END
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


def _make_tool_node(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> Callable[[AgentState], Any]:
    async def tool_node(state: AgentState) -> dict:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)

        new_messages: list[ToolMessage] = []
        tool_call_count = state["tool_call_count"]
        invalid_call_count = state["invalid_call_count"]
        terminated_reason: str | None = None
        round_num = state["round_count"]

        for call in last.tool_calls:
            # Every branch below funnels through the bottom of this loop
            # body rather than `continue`-ing early, specifically so the
            # invalid-call-limit check at the end always runs exactly once
            # per call regardless of which branch handled it — an earlier
            # version used `continue` in the unknown-tool/invalid-argument
            # branches and silently skipped that check for them, letting a
            # model that only ever requested unknown tools loop all the way
            # to max_agent_rounds instead of stopping at max_invalid_tool_calls.
            if terminated_reason is not None or tool_call_count >= settings.max_tool_calls:
                terminated_reason = terminated_reason or "max_tool_calls_exceeded"
                new_messages.append(
                    ToolMessage(
                        content="Not executed: this session's tool-call budget is exhausted.",
                        tool_call_id=call["id"],
                    )
                )
                continue  # budget exhaustion isn't an invalid-call condition

            spec = TOOL_REGISTRY.get(call["name"])
            if spec is None:
                invalid_call_count += 1
                async with session_factory() as session:
                    await record_event(
                        session,
                        session_id=state["session_id"],
                        round_num=round_num,
                        event_type="tool_requested",
                        status=EventStatus.REJECTED,
                        tool_name=call["name"],
                        error_category="unknown_tool",
                    )
                    await session.commit()
                new_messages.append(
                    ToolMessage(content=f"Unknown tool {call['name']!r}", tool_call_id=call["id"])
                )
                if invalid_call_count >= settings.max_invalid_tool_calls:
                    terminated_reason = "too_many_invalid_calls"
                continue

            try:
                validated_args = spec.args_schema.model_validate(call["args"])
            except ValidationError as exc:
                invalid_call_count += 1
                async with session_factory() as session:
                    await record_event(
                        session,
                        session_id=state["session_id"],
                        round_num=round_num,
                        event_type="tool_requested",
                        status=EventStatus.REJECTED,
                        tool_name=spec.name,
                        arguments=call["args"],
                        error_category="invalid_argument",
                    )
                    await session.commit()
                new_messages.append(
                    ToolMessage(content=f"Invalid arguments: {exc}", tool_call_id=call["id"])
                )
                if invalid_call_count >= settings.max_invalid_tool_calls:
                    terminated_reason = "too_many_invalid_calls"
                continue

            start = time.monotonic()
            async with session_factory() as session:
                try:
                    result = await asyncio.wait_for(
                        spec.handler(session, state["session_id"], validated_args),
                        timeout=settings.tool_timeout_seconds,
                    )
                except TimeoutError:
                    await session.rollback()
                    tool_call_count += 1
                    latency_ms = int((time.monotonic() - start) * 1000)
                    await record_event(
                        session,
                        session_id=state["session_id"],
                        round_num=round_num,
                        event_type="tool_executed",
                        status=EventStatus.FAILURE,
                        tool_name=spec.name,
                        arguments=call["args"],
                        latency_ms=latency_ms,
                        error_category="timeout",
                    )
                    await session.commit()
                    new_messages.append(
                        ToolMessage(content="Tool call timed out", tool_call_id=call["id"])
                    )
                except GroundingRejectedError as exc:
                    await session.rollback()
                    tool_call_count += 1
                    invalid_call_count += 1
                    latency_ms = int((time.monotonic() - start) * 1000)
                    await record_event(
                        session,
                        session_id=state["session_id"],
                        round_num=round_num,
                        event_type="tool_executed",
                        status=EventStatus.REJECTED,
                        tool_name=spec.name,
                        arguments=call["args"],
                        latency_ms=latency_ms,
                        error_category="grounding_rejected",
                    )
                    await session.commit()
                    new_messages.append(ToolMessage(content=str(exc), tool_call_id=call["id"]))
                except ToolExecutionError as exc:
                    await session.rollback()
                    tool_call_count += 1
                    latency_ms = int((time.monotonic() - start) * 1000)
                    await record_event(
                        session,
                        session_id=state["session_id"],
                        round_num=round_num,
                        event_type="tool_executed",
                        status=EventStatus.FAILURE,
                        tool_name=spec.name,
                        arguments=call["args"],
                        latency_ms=latency_ms,
                        error_category=exc.category,
                    )
                    await session.commit()
                    new_messages.append(ToolMessage(content=f"Error: {exc}", tool_call_id=call["id"]))
                else:
                    await session.commit()
                    tool_call_count += 1
                    latency_ms = int((time.monotonic() - start) * 1000)
                    await record_event(
                        session,
                        session_id=state["session_id"],
                        round_num=round_num,
                        event_type="tool_executed",
                        status=EventStatus.SUCCESS,
                        tool_name=spec.name,
                        arguments=call["args"],
                        latency_ms=latency_ms,
                    )
                    await session.commit()
                    new_messages.append(
                        ToolMessage(content=json.dumps(result, default=str), tool_call_id=call["id"])
                    )

            if invalid_call_count >= settings.max_invalid_tool_calls:
                terminated_reason = "too_many_invalid_calls"

        return {
            "messages": new_messages,
            "tool_call_count": tool_call_count,
            "invalid_call_count": invalid_call_count,
            "terminated_reason": terminated_reason,
        }

    return tool_node


def _route_after_tools(state: AgentState) -> str:
    if state.get("terminated_reason"):
        return END
    return "agent"


def build_graph(
    chat_model: BaseChatModel, session_factory: async_sessionmaker[AsyncSession], settings: Settings
):
    graph = StateGraph(AgentState)
    graph.add_node("agent", _make_agent_node(chat_model, session_factory, settings))
    graph.add_node("tools", _make_tool_node(session_factory, settings))
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", _route_after_tools, {"agent": "agent", END: END})
    return graph.compile()


async def run_agent(
    *,
    session_id: str,
    user_text: str,
    chat_model: BaseChatModel,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    history: list[Any] | None = None,
) -> AgentState:
    # Session identity (concept 34) is established here, once, regardless of
    # which entrance (API route, test, script) invoked the agent — every
    # AgentEvent recorded during the run has a foreign key to this row.
    async with session_factory() as session:
        await SessionRepository(session).get_or_create(session_id)
        await session.commit()

    compiled = build_graph(chat_model, session_factory, settings)
    # `history` (concepts 35, 42): prior turns from ConversationMessage,
    # already converted to LangChain messages by the caller — this is what
    # lets the agent resolve "that appointment" / "the next one" against
    # what was actually said earlier in the session, instead of starting
    # fresh on every request.
    initial_state: AgentState = {
        "session_id": session_id,
        "messages": [*(history or []), HumanMessage(content=user_text)],
        "round_count": 0,
        "tool_call_count": 0,
        "invalid_call_count": 0,
        "terminated_reason": None,
    }
    # Generous relative to max_agent_rounds: our own bounds always trigger
    # first, this is only a hard backstop against a bug in that logic.
    return await compiled.ainvoke(
        initial_state, config={"recursion_limit": settings.max_agent_rounds * 4 + 10}
    )

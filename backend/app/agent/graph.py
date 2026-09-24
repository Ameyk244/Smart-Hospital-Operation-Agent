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

from app.agent.entity_codes import extract_entity_codes
from app.agent.grounding import GroundingRejectedError
from app.agent.state import AgentState
from app.agent.tools import TOOL_REGISTRY, ToolExecutionError, all_tool_dicts
from app.config import Settings
from app.db.models.agent import EventStatus
from app.db.repositories.preference_repository import PreferenceRepository
from app.db.repositories.session_repository import SessionRepository
from app.observability.tracing import record_event

SYSTEM_PROMPT = (
    "You are the Smart Hospital Operations Agent, assisting hospital staff with "
    "scheduling and scanner assignment questions over synthetic data. You have "
    "tools to search appointments (search_appointments) and scanners "
    "(search_scanners), run other known read-only "
    "lookups (execute_command — departments, scanners, patient lookup, next "
    "appointment), and reschedule an appointment to a different scanner "
    "(reschedule_appointment). Always search before acting: you may only pass "
    "appointment_code/scanner_code values into reschedule_appointment that came from "
    "a search or execute_command result earlier in this conversation — a value you "
    "make up will be rejected. If a tool call is rejected or fails, explain why to "
    "the user rather than retrying the same invented value. Answer a question about "
    "a department, modality, status, scanner or patient with a fresh search using "
    "the matching filter (department_code, appointment_type, scanner_code, ...), "
    "not from earlier results, unless the user is explicitly asking about those "
    "earlier results. You also have "
    "remember_preference/forget_preference/list_preferences tools for this session's "
    "explicit preferences — use them only when the user actually asks you to "
    "remember, forget, or recall something; never save a preference on your own "
    "initiative. If a request bundles a clearly non-hospital sub-task with (or "
    "instead of) a hospital-relevant one — arithmetic, creative writing, general "
    "knowledge, or anything outside this tool surface — answer only the "
    "hospital-relevant part, if any, and explicitly decline the rest; do not do the "
    "non-hospital part anyway just to be helpful or friendly. Staying strictly "
    "within hospital-operations scheduling and scanner assignment is the goal here, "
    "not maximum helpfulness, even when that means declining part of a request. Be "
    "concise. Write in plain, readable sentences — use bold only for something that "
    "genuinely needs emphasis, not on every entity name, and use a table only when "
    "you're presenting several rows of comparable data (e.g. a list of "
    "appointments), not for a single fact."
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
                model_with_tools.ainvoke(state["messages"]), timeout=settings.llm_timeout_seconds
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
        # Carried forward across rounds *within this turn* (tool_node can
        # run several times before the loop ends) but reset to [] fresh at
        # the start of every new run_agent() call — see state.py's comment
        # on why this deliberately isn't a reducer-merged field.
        touched_entity_codes = list(state.get("touched_entity_codes", []))
        touched_seen = set(touched_entity_codes)

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
                    for code in extract_entity_codes(result):
                        if code not in touched_seen:
                            touched_seen.add(code)
                            touched_entity_codes.append(code)
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
            "touched_entity_codes": touched_entity_codes,
        }

    return tool_node


def _route_after_tools(state: AgentState) -> str:
    if state.get("terminated_reason"):
        return END
    return "agent"


def build_graph(
    chat_model: BaseChatModel,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    checkpointer: Any | None = None,
):
    graph = StateGraph(AgentState)
    graph.add_node("agent", _make_agent_node(chat_model, session_factory, settings))
    graph.add_node("tools", _make_tool_node(session_factory, settings))
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", _route_after_tools, {"agent": "agent", END: END})
    return graph.compile(checkpointer=checkpointer)


async def _build_system_message(
    session_factory: async_sessionmaker[AsyncSession], session_id: str
) -> SystemMessage:
    """Base system prompt, plus this session's remembered preferences
    (concept 40: memory injection) when there are any — never fabricated,
    only what the `remember_preference` tool actually wrote for this exact
    session_id."""
    async with session_factory() as session:
        prefs = await PreferenceRepository(session).list_for_session(session_id)
    if not prefs:
        return SystemMessage(content=SYSTEM_PROMPT)
    pref_lines = "\n".join(f"- {p.key}: {p.value}" for p in prefs)
    return SystemMessage(
        content=(
            f"{SYSTEM_PROMPT}\n\n"
            "Remembered preferences for this session (apply only where relevant to "
            f"the current request, don't force them into unrelated answers):\n{pref_lines}"
        )
    )


async def run_agent(
    *,
    session_id: str,
    user_text: str,
    chat_model: BaseChatModel,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    history: list[Any] | None = None,
    checkpointer: Any | None = None,
) -> AgentState:
    # Session identity (concept 34) is established here, once, regardless of
    # which entrance (API route, test, script) invoked the agent — every
    # AgentEvent recorded during the run has a foreign key to this row.
    async with session_factory() as session:
        await SessionRepository(session).get_or_create(session_id)
        await session.commit()

    compiled = build_graph(chat_model, session_factory, settings, checkpointer=checkpointer)
    recursion_limit = settings.max_agent_rounds * 4 + 10

    if checkpointer is not None:
        # Checkpointing (concept 36) is the primary history mechanism here,
        # not `history` from ConversationMessage — passing both would
        # duplicate the transcript, since freshly-constructed LangChain
        # messages have no id LangGraph could use to recognize them as
        # already present in the checkpoint (the same class of pitfall as
        # the FakeMessagesListChatModel issue documented in
        # tests/integration/test_agent_loop.py). `thread_id=session_id`
        # means a second run_agent call for the same session automatically
        # resumes with this thread's prior messages already in state.
        config = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": recursion_limit,
        }
        existing = await compiled.aget_state(config)
        is_new_thread = not existing.values
        system_message = await _build_system_message(session_factory, session_id)
        messages: list[Any] = (
            [system_message] if is_new_thread else []
        ) + [HumanMessage(content=user_text)]
    else:
        config = {"recursion_limit": recursion_limit}
        system_message = await _build_system_message(session_factory, session_id)
        # `history` (concepts 35, 42): prior turns from ConversationMessage,
        # already converted to LangChain messages by the caller — the
        # fallback path for when no checkpointer is configured, so a
        # multi-turn conversation still has continuity.
        messages = [system_message, *(history or []), HumanMessage(content=user_text)]

    # round/tool/invalid counters and terminated_reason are explicitly reset
    # to their starting values on every call, checkpointer or not — these
    # bounds are per-turn, not cumulative across a thread's entire lifetime.
    initial_state: AgentState = {
        "session_id": session_id,
        "messages": messages,
        "round_count": 0,
        "tool_call_count": 0,
        "invalid_call_count": 0,
        "terminated_reason": None,
        "touched_entity_codes": [],
    }
    return await compiled.ainvoke(initial_state, config=config)


class _StateOnlyModel:
    """Stands in for the chat model when a graph is built only to write state.
    `record_non_agent_turn` never runs a node, so `bind_tools` is the only
    method touched (at build time) and no LLM client is ever constructed."""

    def bind_tools(self, _tools: Any) -> "_StateOnlyModel":
        return self


async def record_non_agent_turn(
    *,
    session_id: str,
    user_text: str,
    assistant_text: str,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    checkpointer: Any | None,
) -> None:
    """Write a turn the parser or Jev answered into the agent's checkpoint.

    Why: with a checkpointer, the graph state is the *only* history the agent
    sees (see `run_agent`), and it used to contain only agent-handled turns.
    After `list delayed appointments` was answered deterministically, a
    follow-up such as "what did you just find?" reached an agent that had
    never seen either message and answered "I haven't run any searches yet".
    Recording the exchange keeps one conversation, whichever path answered.

    No-op without a checkpointer: then `run_agent` gets the full transcript
    from `ConversationMessage` rows, which already include these turns.
    Only the user text and the reply text are written, never tool state: the
    exchange grounds no entity codes, exactly as before.
    """
    if checkpointer is None:
        return
    compiled = build_graph(_StateOnlyModel(), session_factory, settings, checkpointer=checkpointer)  # type: ignore[arg-type]
    config = {"configurable": {"thread_id": session_id}}
    existing = await compiled.aget_state(config)
    messages: list[Any] = []
    if not existing.values:
        # Same first-message rule as run_agent: a thread's first message is
        # the system prompt, and run_agent skips adding it once state exists.
        messages.append(await _build_system_message(session_factory, session_id))
    messages += [HumanMessage(content=user_text), AIMessage(content=assistant_text)]
    await compiled.aupdate_state(config, {"messages": messages}, as_node="agent")

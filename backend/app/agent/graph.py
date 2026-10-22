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
import time
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.skills import select_skills
from app.agent.state import AgentState
from app.agent.tools import all_tool_dicts
from app.agent.turn import ToolTurn
from app.config import Settings
from app.db.models.agent import EventStatus
from app.db.repositories.preference_repository import PreferenceRepository
from app.db.repositories.session_repository import SessionRepository
from app.observability import metrics, spans
from app.observability.tracing import record_event

SYSTEM_PROMPT = (
    "You are the Smart Hospital Operations Agent, the assistant of this hospital "
    "operations application: you help hospital staff with scheduling and scanner "
    "assignment over synthetic data, answer questions about how the application and "
    "its features work, and explain the hospital-operations concepts it deals with "
    "(what MRI, CT and X-ray scanners are, what an appointment status means). You have "
    "tools to search appointments (search_appointments) and scanners "
    "(search_scanners), run other known read-only "
    "lookups (execute_command — departments, scanners, patient lookup, next "
    "appointment), and reschedule an appointment to a different scanner "
    "(reschedule_appointment). For the emergency department you can list beds "
    "(search_beds), the emergency queue (search_queue), show a patient's journey "
    "(get_patient_journey), admit a waiting patient to a bed (assign_bed) and "
    "release a bed (release_bed) and mark a cleaned bed available again "
    "(mark_bed_clean). Always search before acting: you may only pass "
    "appointment_code/scanner_code/patient_code/bed_code values into a write tool that "
    "came from a search or execute_command result earlier in this conversation — a "
    "value you make up will be rejected. If a tool call is rejected or fails, explain why to "
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
    "knowledge, or anything unrelated to this application — answer only the "
    "hospital-relevant part, if any, and explicitly decline the rest; do not do the "
    "non-hospital part anyway just to be helpful or friendly. Staying within this "
    "application and its hospital-operations domain is the goal here, "
    "not maximum helpfulness, even when that means declining part of a request. "
    "About the application, when asked: exact commands such as \"list scanners mri "
    "available\" run instantly without AI; near-miss phrasings of those are mapped to "
    "them by Jev; everything else comes to you. The left panel browses departments, "
    "scanners, appointments and patients; the trace panel shows what was done for "
    "each message; the header lights show whether the agent, the harness and Jev are "
    "on (green), off or not configured (grey) or not answering (red); when confirmation is required, a reschedule or bed change is only proposed and the user "
    "presses Confirm or Cancel in the chat; codes (SCN-5, APT-3004, PT-1020) must "
    "appear in a search result before they can be used (grounding). Do not describe "
    "features you are not sure exist. Be "
    "concise. Write in plain, readable sentences — use bold only for something that "
    "genuinely needs emphasis, not on every entity name, and use a table only when "
    "you're presenting several rows of comparable data (e.g. a list of "
    "appointments), not for a single fact. If the message is only a name, an "
    "entity code (a patient name, SCN-5, APT-3004) or a short word with no request, "
    "do not guess a task: look the entity up with the matching tool, tell the user "
    "briefly what you found, offer two to four things you can do next (for example "
    "show their appointments, check a scanner, reschedule an appointment) and ask "
    "what they want. A greeting or \"what can you do?\" gets a short list of what you "
    "can do: look up patients, appointments, scanners and departments, reschedule an "
    "appointment, and remember preferences. Never change anything on a bare mention."
)

WATCH_PROMPT = (
    " You can also list and create watches (list_watches, create_watch), stop one "
    "(stop_watch), and list and acknowledge alerts (list_alerts, acknowledge_alert); a watch "
    "is a read-only monitor that only raises alerts. task_code and alert_id values are "
    "grounded like the codes above: they must come from list_watches, create_watch or "
    "list_alerts results earlier in this conversation."
)


def _model_label(settings: Settings) -> str:
    """The configured model name: one fixed value per process, so it is
    safe as a metric attribute."""
    if settings.llm_provider == "openrouter":
        return settings.openrouter_model
    return settings.anthropic_model


class _RoundSpans:
    """Holds the open `agent.round` span (observability Phase 4).

    A round is one model call plus the tool calls it asked for, but those
    run in two different graph nodes, and a context manager can't stay open
    across nodes. So agent_node starts the round span here (current while
    agent_node runs), tool_node parents its `tool.execute` spans on it
    explicitly, and the round ends when its work does: at the end of
    agent_node if the model asked for no tools, else at the end of
    tool_node. `run_agent`'s finally is only a safety net. Ending it there
    used to make the last round absorb the turn's final checkpoint writes.
    One instance per graph build, so
    concurrent turns never share it."""

    def __init__(self) -> None:
        self.round: trace.Span | None = None
        self._round_token: object | None = None

    def start_round(self, round_num: int) -> None:
        """Starts the round span and makes it *current* until
        `deactivate_round`, so log lines written in agent_node carry its
        span_id, not agent.run's."""
        self.end()
        self.round = spans.get_tracer().start_span(
            "agent.round", attributes={"agent.round": round_num}
        )
        self._round_token = otel_context.attach(trace.set_span_in_context(self.round))

    def deactivate_round(self) -> None:
        """Detach (don't end) the round span. Called at the end of
        agent_node, in the same context that attached it."""
        if self._round_token is not None:
            otel_context.detach(self._round_token)
            self._round_token = None

    def context(self) -> Context | None:
        return trace.set_span_in_context(self.round) if self.round is not None else None

    def end(self) -> None:
        if self.round is not None:
            self.round.end()
            self.round = None


def _make_agent_node(
    chat_model: BaseChatModel,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    round_spans: _RoundSpans,
) -> Callable[[AgentState], Any]:
    model_with_tools = chat_model.bind_tools(all_tool_dicts(settings))
    model_label = _model_label(settings)

    async def invoke_model(messages: list[Any], round_num: int) -> AIMessage | None:
        """The LLM call inside an `llm.invoke` span. None means it timed
        out. Attributes: model name, round, status, token counts."""
        with spans.span(
            "llm.invoke",
            {"gen_ai.request.model": model_label, "agent.round": round_num},
            context=round_spans.context(),
        ) as llm_span:
            llm_started = time.perf_counter()
            try:
                response = await asyncio.wait_for(
                    model_with_tools.ainvoke(messages), timeout=settings.llm_timeout_seconds
                )
            except TimeoutError:
                metrics.record_llm_call(time.perf_counter() - llm_started, "timeout", model_label)
                llm_span.set_attribute("llm.status", "timeout")
                spans.mark_error(llm_span, "timeout")
                return None
            except Exception:
                # Propagates (see module docstring), but is still counted.
                metrics.record_llm_call(time.perf_counter() - llm_started, "error", model_label)
                llm_span.set_attribute("llm.status", "error")
                raise
            usage = getattr(response, "usage_metadata", None)
            metrics.record_llm_call(time.perf_counter() - llm_started, "ok", model_label, usage)
            spans.set_attributes(
                llm_span,
                **{
                    "llm.status": "ok",
                    "gen_ai.usage.input_tokens": (usage or {}).get("input_tokens"),
                    "gen_ai.usage.output_tokens": (usage or {}).get("output_tokens"),
                    "llm.tool_call_count": len(response.tool_calls),
                },
            )
            return response

    async def agent_node(state: AgentState) -> dict:
        try:
            update = await run_round(state)
        finally:
            round_spans.deactivate_round()
        last = update.get("messages", [None])[-1]
        if update.get("terminated_reason") or not (isinstance(last, AIMessage) and last.tool_calls):
            # No tools to run: this round is finished now, so it doesn't
            # absorb the final checkpoint writes that follow.
            round_spans.end()
        return update

    async def run_round(state: AgentState) -> dict:
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
        round_spans.start_round(round_num)
        async with session_factory() as session:
            await record_event(
                session,
                session_id=state["session_id"],
                round_num=round_num,
                event_type="agent_invoked",
                status=EventStatus.SUCCESS,
            )
            await session.commit()

        response = await invoke_model(state["messages"], round_num)
        if response is None:
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

        arguments = {"tool_call_count": len(response.tool_calls)} if response.tool_calls else None
        usage = response.usage_metadata
        if usage and isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int):
            arguments = {
                **(arguments or {}),
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            }
        async with session_factory() as session:
            await record_event(
                session,
                session_id=state["session_id"],
                round_num=round_num,
                event_type="llm_response",
                status=EventStatus.SUCCESS,
                arguments=arguments,
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
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    round_spans: _RoundSpans,
) -> Callable[[AgentState], Any]:
    async def tool_node(state: AgentState) -> dict:
        try:
            return await run_tools(state)
        finally:
            # The round is complete once its tools have run.
            round_spans.end()

    async def run_tools(state: AgentState) -> dict:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)

        turn = ToolTurn(
            session_id=state["session_id"],
            session_factory=session_factory,
            settings=settings,
            tool_call_count=state["tool_call_count"],
            invalid_call_count=state["invalid_call_count"],
            write_call_count=state["write_call_count"],
            touched_entity_codes=state.get("touched_entity_codes", []),
        )
        new_messages = [
            ToolMessage(
                content=await turn.call(
                    name=call["name"],
                    args=call["args"],
                    round_num=state["round_count"],
                    parent_context=round_spans.context(),
                ),
                tool_call_id=call["id"],
            )
            for call in last.tool_calls
        ]
        return {
            "messages": new_messages,
            "tool_call_count": turn.tool_call_count,
            "invalid_call_count": turn.invalid_call_count,
            "write_call_count": turn.write_call_count,
            "terminated_reason": turn.terminated_reason,
            "touched_entity_codes": turn.touched_entity_codes,
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
    round_spans: _RoundSpans | None = None,
):
    round_spans = round_spans or _RoundSpans()
    graph = StateGraph(AgentState)
    graph.add_node("agent", _make_agent_node(chat_model, session_factory, settings, round_spans))
    graph.add_node("tools", _make_tool_node(session_factory, settings, round_spans))
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", _route_after_tools, {"agent": "agent", END: END})
    return graph.compile(checkpointer=checkpointer)


async def build_system_message(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
    settings: Settings,
    user_text: str | None = None,
) -> SystemMessage:
    """Base system prompt, plus this session's remembered preferences
    (concept 40: memory injection) when there are any — never fabricated,
    only what the `remember_preference` tool actually wrote for this exact
    session_id — plus task guidance for `user_text` when one is given."""
    async with session_factory() as session:
        prefs = await PreferenceRepository(session).list_for_session(session_id)
    content = SYSTEM_PROMPT + (WATCH_PROMPT if settings.scheduler_enabled else "")
    if prefs:
        pref_lines = "\n".join(f"- {p.key}: {p.value}" for p in prefs)
        content += (
            "\n\nRemembered preferences for this session (apply only where relevant to "
            f"the current request, don't force them into unrelated answers):\n{pref_lines}"
        )
    if user_text and (skills := select_skills(user_text, settings.scheduler_enabled)):
        content += f"\n\nTask guidance for this request:\n{skills}"
    return SystemMessage(content=content)


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

    round_spans = _RoundSpans()
    compiled = build_graph(
        chat_model, session_factory, settings, checkpointer=checkpointer, round_spans=round_spans
    )
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
        system_message = await build_system_message(session_factory, session_id, settings)
        messages: list[Any] = (
            [system_message] if is_new_thread else []
        ) + [HumanMessage(content=user_text)]
    else:
        config = {"recursion_limit": recursion_limit}
        # Rebuilt every turn here, so skills can follow the message; with a
        # checkpointer the system message is fixed when the thread starts.
        system_message = await build_system_message(
            session_factory,
            session_id,
            settings,
            user_text if settings.agent_skills_enabled else None,
        )
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
        "write_call_count": 0,
        "terminated_reason": None,
        "touched_entity_codes": [],
    }
    # `agent.run` is current while the graph runs; LangGraph runs each node
    # in a task that copies the context, so `agent.round` spans started in
    # agent_node are its children.
    with spans.span("agent.run", {"session.id": session_id}) as run_span:
        try:
            final_state = await compiled.ainvoke(initial_state, config=config)
        finally:
            round_spans.end()
        termination = final_state.get("terminated_reason") or "completed"
        spans.set_attributes(
            run_span,
            **{
                "agent.rounds": final_state["round_count"],
                "agent.tool_calls": final_state["tool_call_count"],
                "agent.invalid_calls": final_state["invalid_call_count"],
                "agent.termination": termination,
            },
        )
    # Counted here, from final state, because two of the terminations
    # (max_tool_calls_exceeded, too_many_invalid_calls) never write an
    # agent_events row; this is the only place all five are visible.
    metrics.record_agent_turn(final_state["round_count"], final_state.get("terminated_reason"))
    return final_state


class _StateOnlyModel:
    """Stands in for the chat model when a graph is built only to write state.
    `record_non_agent_turn` never runs a node, so `bind_tools` is the only
    method touched (at build time) and no LLM client is ever constructed."""

    def bind_tools(self, _tools: Any) -> "_StateOnlyModel":
        return self


def _state_graph(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, checkpointer: Any
):
    return build_graph(_StateOnlyModel(), session_factory, settings, checkpointer=checkpointer)  # type: ignore[arg-type]


async def get_thread_messages(
    *,
    session_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    checkpointer: Any,
) -> list[Any]:
    """The checkpoint thread's stored messages, empty for a new thread."""
    compiled = _state_graph(session_factory, settings, checkpointer)
    existing = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    return list(existing.values.get("messages", []))


async def record_turn_messages(
    *,
    session_id: str,
    messages: list[Any],
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    checkpointer: Any | None,
) -> None:
    """Append already-finished messages to the session's checkpoint thread.

    No-op without a checkpointer: then `run_agent` gets the full transcript
    from `ConversationMessage` rows. Never runs a node, so no model is built.
    """
    if checkpointer is None:
        return
    compiled = _state_graph(session_factory, settings, checkpointer)
    config = {"configurable": {"thread_id": session_id}}
    existing = await compiled.aget_state(config)
    to_write: list[Any] = []
    if not existing.values:
        # Same first-message rule as run_agent: a thread's first message is
        # the system prompt, and run_agent skips adding it once state exists.
        to_write.append(await build_system_message(session_factory, session_id, settings))
    await compiled.aupdate_state(config, {"messages": to_write + messages}, as_node="agent")


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

    Only the user text and the reply text are written, never tool state: the
    exchange grounds no entity codes, exactly as before.
    """
    await record_turn_messages(
        session_id=session_id,
        messages=[HumanMessage(content=user_text), AIMessage(content=assistant_text)],
        session_factory=session_factory,
        settings=settings,
        checkpointer=checkpointer,
    )

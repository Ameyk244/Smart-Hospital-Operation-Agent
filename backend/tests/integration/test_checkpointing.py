"""LangGraph Postgres checkpointing (concept 36): proves a second,
independent `run_agent` call sharing a thread_id resumes from where the
first left off — the message history included — without the caller
manually reconstructing it via `history`. This is the mechanism that's
supposed to be distinct from `ConversationMessage` (concept 35) and
`Preference` (concept 37); this test exercises it in isolation, with no
`history` argument passed at all, so a pass can only be explained by the
checkpointer actually working.
"""

import pytest
from langchain_core.messages import AIMessage

from app.agent.graph import run_agent
from tests.integration.test_agent_loop import ScriptedChatModel, _settings

pytestmark = pytest.mark.integration


async def test_second_run_resumes_prior_messages_via_checkpointer(
    agent_session_factory, checkpointer
):
    model = ScriptedChatModel(responses=[AIMessage(content="First answer.")])

    first_state = await run_agent(
        session_id="test-checkpoint-resume",
        user_text="What departments exist?",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
        checkpointer=checkpointer,
    )
    assert len(first_state["messages"]) == 3  # system, human, ai

    model2 = ScriptedChatModel(responses=[AIMessage(content="Second answer.")])
    second_state = await run_agent(
        session_id="test-checkpoint-resume",
        user_text="And what about scanners?",
        chat_model=model2,
        session_factory=agent_session_factory,
        settings=_settings(),
        checkpointer=checkpointer,
    )

    # No system message duplicated, and the first turn's human/ai messages
    # are present even though this call never received `history`.
    contents = [m.content for m in second_state["messages"]]
    assert "What departments exist?" in contents
    assert "First answer." in contents
    assert "And what about scanners?" in contents
    assert "Second answer." in contents
    system_messages = [m for m in second_state["messages"] if m.type == "system"]
    assert len(system_messages) == 1


async def test_bounds_reset_each_turn_despite_shared_thread(agent_session_factory, checkpointer):
    """round_count/tool_call_count/invalid_call_count must not accumulate
    across turns sharing a thread_id — those bounds are per-invocation, not
    per-thread-lifetime. If checkpointing were naively resuming the whole
    state including these counters, a long conversation would eventually
    hit max_agent_rounds within a handful of ordinary turns."""
    for i in range(3):
        state = await run_agent(
            session_id="test-checkpoint-bounds-reset",
            user_text=f"turn {i}",
            chat_model=ScriptedChatModel(responses=[AIMessage(content=f"ok {i}")]),
            session_factory=agent_session_factory,
            settings=_settings(max_agent_rounds=2),
            checkpointer=checkpointer,
        )
        assert state["round_count"] == 1
        assert state["terminated_reason"] is None


async def test_touched_entity_codes_reset_each_turn_despite_shared_thread(
    agent_session_factory, checkpointer
):
    """Same pitfall as round_count/tool_call_count, applied to Task 4's
    touched_entity_codes: a turn that touches nothing must not inherit
    codes a *previous* turn on the same thread touched."""
    first_state = await run_agent(
        session_id="test-checkpoint-touched-reset",
        user_text="search for delayed MRI appointments",
        chat_model=ScriptedChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "search_appointments",
                            "args": {"appointment_type": "MRI", "status": "DELAYED"},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Found them."),
            ]
        ),
        session_factory=agent_session_factory,
        settings=_settings(),
        checkpointer=checkpointer,
    )
    assert "APT-2001" in first_state["touched_entity_codes"]

    second_state = await run_agent(
        session_id="test-checkpoint-touched-reset",
        user_text="just say hello, don't call any tool",
        chat_model=ScriptedChatModel(responses=[AIMessage(content="hello")]),
        session_factory=agent_session_factory,
        settings=_settings(),
        checkpointer=checkpointer,
    )
    assert second_state["touched_entity_codes"] == []


async def test_different_thread_ids_do_not_share_state(agent_session_factory, checkpointer):
    model_a = ScriptedChatModel(responses=[AIMessage(content="Answer for A.")])
    model_b = ScriptedChatModel(responses=[AIMessage(content="Answer for B.")])

    await run_agent(
        session_id="test-checkpoint-thread-a",
        user_text="Thread A message",
        chat_model=model_a,
        session_factory=agent_session_factory,
        settings=_settings(),
        checkpointer=checkpointer,
    )
    state_b = await run_agent(
        session_id="test-checkpoint-thread-b",
        user_text="Thread B message",
        chat_model=model_b,
        session_factory=agent_session_factory,
        settings=_settings(),
        checkpointer=checkpointer,
    )

    contents_b = [m.content for m in state_b["messages"]]
    assert "Thread A message" not in contents_b
    assert "Answer for A." not in contents_b

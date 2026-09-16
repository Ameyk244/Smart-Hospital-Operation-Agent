"""Memory tools + preference injection (concepts 37, 38, 40, 41): explicit
remember/forget/list tools, and proof that a preference set before a run
actually lands in the system message the model sees — not just that it's
stored, but that it's *used*.
"""

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from app.agent.graph import run_agent
from app.db.repositories.preference_repository import PreferenceRepository
from app.db.repositories.session_repository import SessionRepository
from tests.integration.test_agent_loop import ScriptedChatModel, _settings

pytestmark = pytest.mark.integration


async def test_remember_then_list_preference(agent_session_factory):
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "remember_preference",
                        "args": {"key": "preferred_scanner_type", "value": "MRI"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "list_preferences", "args": {}, "id": "call_2", "type": "tool_call"}
                ],
            ),
            AIMessage(content="Noted and confirmed."),
        ]
    )

    final_state = await run_agent(
        session_id="test-remember-list",
        user_text="Remember that I prefer MRI scanners, then confirm what you remember.",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert final_state["invalid_call_count"] == 0
    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert "preferred_scanner_type" in tool_messages[0].content
    assert "MRI" in tool_messages[1].content

    async with agent_session_factory() as session:
        prefs = await PreferenceRepository(session).list_for_session("test-remember-list")
    assert {p.key: p.value for p in prefs} == {"preferred_scanner_type": "MRI"}


async def test_forget_preference(agent_session_factory):
    async with agent_session_factory() as session:
        await SessionRepository(session).get_or_create("test-forget")
        await PreferenceRepository(session).remember("test-forget", "workflow", "batch")
        await session.commit()

    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "forget_preference",
                        "args": {"key": "workflow"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Forgotten."),
        ]
    )

    final_state = await run_agent(
        session_id="test-forget",
        user_text="Forget my workflow preference.",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert '"removed": true' in tool_messages[0].content

    async with agent_session_factory() as session:
        prefs = await PreferenceRepository(session).list_for_session("test-forget")
    assert prefs == []


async def test_existing_preferences_are_injected_into_system_message(agent_session_factory):
    async with agent_session_factory() as session:
        await SessionRepository(session).get_or_create("test-injection")
        await PreferenceRepository(session).remember(
            "test-injection", "preferred_scanner_type", "CT"
        )
        await session.commit()

    model = ScriptedChatModel(responses=[AIMessage(content="ok")])

    final_state = await run_agent(
        session_id="test-injection",
        user_text="What's the status of MRI scanners?",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    system_message = final_state["messages"][0]
    assert isinstance(system_message, SystemMessage)
    assert "preferred_scanner_type: CT" in system_message.content


async def test_no_preferences_means_plain_system_prompt(agent_session_factory):
    model = ScriptedChatModel(responses=[AIMessage(content="ok")])

    final_state = await run_agent(
        session_id="test-no-prefs",
        user_text="hello",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    system_message = final_state["messages"][0]
    assert isinstance(system_message, SystemMessage)
    assert "Remembered preferences" not in system_message.content

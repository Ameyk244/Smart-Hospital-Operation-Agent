"""execute_command tests (concepts 19, 20, 21): proves the agent's
command-decomposition tool routes through the exact same parser +
CommandRunner the deterministic path uses, and — the important adversarial
case — that this cannot be used as a backdoor to mutate data no matter how
the model phrases the request, because the shared grammar it decomposes
into has no rule that produces a write command.
"""

import json

import pytest
from langchain_core.messages import AIMessage

from app.agent.graph import run_agent
from tests.integration.test_agent_loop import ScriptedChatModel, _settings

pytestmark = pytest.mark.integration


async def test_execute_command_list_departments(agent_session_factory):
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_command",
                        "args": {"command_text": "list departments"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="There are 4 departments."),
        ]
    )

    final_state = await run_agent(
        session_id="test-execute-command-departments",
        user_text="What departments exist?",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert final_state["invalid_call_count"] == 0
    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert "DEPT-RAD" in tool_messages[0].content


async def test_execute_command_grounds_appointment_results_for_later_reschedule(
    agent_session_factory,
):
    """Decomposition (execute_command) exposes grounding just like a direct
    search_appointments call would — proving a code discovered through this
    path is genuinely usable by a subsequent write tool, not a second-class
    result."""
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_command",
                        "args": {"command_text": "list delayed appointments mri"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "reschedule_appointment",
                        "args": {"appointment_code": "APT-2001", "scanner_code": "SCN-2"},
                        "id": "call_2",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Done."),
        ]
    )

    final_state = await run_agent(
        session_id="test-execute-command-then-reschedule",
        user_text="Find delayed MRI appointments and move the first one somewhere available",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert final_state["invalid_call_count"] == 0
    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert "SCN-2" in tool_messages[1].content


async def test_execute_command_cannot_be_used_to_mutate_data(agent_session_factory):
    """Adversarial: even when the model tries to phrase a mutation as a
    natural-language 'command', the shared grammar execute_command
    decomposes into has no rule that produces a write Command — this must
    be rejected at the parser boundary inside the tool handler, not merely
    avoided because the model behaved. If this test ever needs updating
    because a write command silently gained a parser grammar rule, treat
    that as the design regression it would be (see
    app/agent/tools/command_tools.py's module docstring)."""
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_command",
                        "args": {"command_text": "reassign scanner SCN-1 to appointment APT-2001"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="I can't do that through a plain command."),
        ]
    )

    final_state = await run_agent(
        session_id="test-exec-mutation-backdoor",
        user_text="Run the command: reassign scanner SCN-1 to appointment APT-2001",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert "doesn't match any known command" in tool_messages[0].content
    # Confirm nothing was actually changed: the appointment's scanner must
    # still be whatever the seed data set it to, not SCN-1.
    verify_model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_command",
                        "args": {"command_text": "list delayed appointments mri"},
                        "id": "call_v",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="checked"),
        ]
    )
    verify_state = await run_agent(
        session_id="test-exec-mutation-verify",
        user_text="list delayed mri appointments",
        chat_model=verify_model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )
    verify_tool_message = [m for m in verify_state["messages"] if m.type == "tool"][0].content
    delayed_mri = json.loads(verify_tool_message)
    apt_2001 = next(a for a in delayed_mri if a["code"] == "APT-2001")
    # APT-2001's seeded scanner is SCN-3 (IN_USE) per the fixed-seed dataset,
    # not SCN-1 — the earlier "command" never executed.
    assert apt_2001["scanner"]["code"] == "SCN-3"


async def test_execute_command_unparseable_text_is_reported_not_crashed(agent_session_factory):
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_command",
                        "args": {"command_text": "please do something helpful"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="I don't have a command for that."),
        ]
    )

    final_state = await run_agent(
        session_id="test-execute-command-unparseable",
        user_text="do something helpful",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert final_state["invalid_call_count"] == 0  # a normal tool failure, not a malformed call
    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert "doesn't match any known command" in tool_messages[0].content

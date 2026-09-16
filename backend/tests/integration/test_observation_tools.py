"""get_scanner_availability tests: happy path (grounded via a prior search)
and the adversarial case — grounding-by-rejection applies to a read tool
just as much as a write tool.
"""

import pytest
from langchain_core.messages import AIMessage

from app.agent.graph import run_agent
from tests.integration.test_agent_loop import ScriptedChatModel, _settings

pytestmark = pytest.mark.integration


async def test_get_scanner_availability_after_search(agent_session_factory):
    model = ScriptedChatModel(
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
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_scanner_availability",
                        "args": {"scanner_code": "SCN-1"},
                        "id": "call_2",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="SCN-1 is available."),
        ]
    )

    final_state = await run_agent(
        session_id="test-obs-happy-path",
        user_text="Is SCN-1 available?",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert final_state["invalid_call_count"] == 0
    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert '"code": "SCN-1"' in tool_messages[1].content


@pytest.mark.adversarial
async def test_get_scanner_availability_rejects_ungrounded_code(agent_session_factory):
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_scanner_availability",
                        "args": {"scanner_code": "SCN-1"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="I can't check that without searching first."),
        ]
    )

    final_state = await run_agent(
        session_id="test-obs-ungrounded",
        user_text="Is SCN-1 available?",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert final_state["invalid_call_count"] == 1
    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert "never exposed" in tool_messages[0].content

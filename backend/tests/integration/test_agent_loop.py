"""Agent-loop tests (concepts 52, 55, 56): the LangGraph loop exercised
against a real database but a *scripted* chat model — no live API calls.
Proves multi-round tool calling, grounding-by-rejection, unknown-tool/
invalid-argument handling, and bounded-execution termination, all
deterministically.
"""

import pytest
from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

from app.agent.graph import run_agent
from app.config import Settings

pytestmark = pytest.mark.integration


class ScriptedChatModel(FakeMessagesListChatModel):
    """A fake chat model that ignores whatever tools are bound to it and
    just cycles through a pre-scripted list of responses — the standard way
    to test a LangGraph tool-calling loop without a live model."""

    def bind_tools(self, tools, **kwargs):
        return self


def _settings(**overrides) -> Settings:
    defaults = dict(
        database_url="postgresql+asyncpg://unused/unused",
        max_agent_rounds=6,
        max_tool_calls=10,
        tool_timeout_seconds=5,
        llm_timeout_seconds=5,
        max_invalid_tool_calls=3,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def test_multi_round_search_then_reschedule(agent_session_factory):
    """The illustrative scenario from the spec: search delayed MRI
    appointments, then reschedule one to a scanner grounded by that same
    search — two tool rounds, then a finishing message with no tool calls."""
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
                        "name": "reschedule_appointment",
                        "args": {"appointment_code": "APT-2001", "scanner_code": "SCN-2"},
                        "id": "call_2",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Done — moved APT-2001 to SCN-2."),
        ]
    )

    final_state = await run_agent(
        session_id="test-happy-path",
        user_text="Find the first delayed MRI appointment and move it to an available scanner",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert final_state["terminated_reason"] is None
    assert final_state["tool_call_count"] == 2
    assert final_state["invalid_call_count"] == 0
    assert "Done" in final_state["messages"][-1].content

    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert len(tool_messages) == 2
    assert "APT-2001" in tool_messages[0].content  # search result includes it
    assert "SCN-2" in tool_messages[1].content  # reschedule result confirms it


@pytest.mark.adversarial
async def test_grounding_rejects_fabricated_appointment_code(agent_session_factory):
    """Adversarial (concept 56): the model tries to reschedule an
    appointment code it never got from a search — must be rejected in code,
    not executed."""
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "reschedule_appointment",
                        "args": {"appointment_code": "APT-9999", "scanner_code": "SCN-1"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="I couldn't do that."),
        ]
    )

    final_state = await run_agent(
        session_id="test-adversarial-grounding",
        user_text="Reschedule APT-9999 to SCN-1",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert final_state["invalid_call_count"] == 1
    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert "never exposed" in tool_messages[0].content


@pytest.mark.adversarial
async def test_unknown_tool_name_is_rejected(agent_session_factory):
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "delete_everything", "args": {}, "id": "call_1", "type": "tool_call"}
                ],
            ),
            AIMessage(content="Never mind."),
        ]
    )

    final_state = await run_agent(
        session_id="test-unknown-tool",
        user_text="do something",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert final_state["invalid_call_count"] == 1
    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert "Unknown tool" in tool_messages[0].content


@pytest.mark.adversarial
async def test_invalid_arguments_are_rejected(agent_session_factory):
    """search_appointments's `limit` field requires 1-100 — a model
    supplying something outside that range should be caught by Pydantic
    validation before any DB call happens."""
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_appointments",
                        "args": {"limit": 999},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="ok"),
        ]
    )

    final_state = await run_agent(
        session_id="test-invalid-args",
        user_text="search",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert final_state["invalid_call_count"] == 1
    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    assert "Invalid arguments" in tool_messages[0].content


def _always_call_tool(name: str, call_id_prefix: str, count: int = 20) -> list[AIMessage]:
    """N distinct AIMessage instances all requesting the same tool call.

    Why distinct instances, not one message cycled: LangGraph's `add_messages`
    reducer assigns an id to a message the first time it's merged into state,
    mutating the object in place. `FakeMessagesListChatModel` returns the
    *same* object reference on every cycle when given a single-element list,
    so a second round would hand back an already-id-tagged message and
    `add_messages` would treat it as an in-place update of the first round's
    message rather than a new append — silently truncating the transcript
    instead of growing it. Distinct objects avoid that collision entirely.
    """
    return [
        AIMessage(
            content="",
            tool_calls=[
                {"name": name, "args": {}, "id": f"{call_id_prefix}_{i}", "type": "tool_call"}
            ],
        )
        for i in range(count)
    ]


async def test_max_rounds_terminates_a_looping_model(agent_session_factory):
    """A model that always requests another tool call must still be cut off
    at max_agent_rounds — bounded execution (concept 27), not a real API
    call that would eventually stop on its own."""
    model = ScriptedChatModel(responses=_always_call_tool("search_appointments", "call"))

    final_state = await run_agent(
        session_id="test-max-rounds",
        user_text="keep searching",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(max_agent_rounds=2, max_tool_calls=100),
    )

    assert final_state["terminated_reason"] == "max_rounds_exceeded"
    assert final_state["round_count"] <= 2


async def test_max_tool_calls_terminates_before_max_rounds(agent_session_factory):
    model = ScriptedChatModel(responses=_always_call_tool("search_appointments", "call"))

    final_state = await run_agent(
        session_id="test-max-tool-calls",
        user_text="keep searching",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(max_agent_rounds=10, max_tool_calls=2),
    )

    assert final_state["terminated_reason"] == "max_tool_calls_exceeded"
    assert final_state["tool_call_count"] <= 2


async def test_too_many_invalid_calls_terminates(agent_session_factory):
    model = ScriptedChatModel(responses=_always_call_tool("not_a_tool", "call"))

    final_state = await run_agent(
        session_id="test-invalid-termination",
        user_text="do nonsense",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(max_agent_rounds=10, max_tool_calls=100, max_invalid_tool_calls=2),
    )

    assert final_state["terminated_reason"] == "too_many_invalid_calls"
    assert final_state["invalid_call_count"] == 2

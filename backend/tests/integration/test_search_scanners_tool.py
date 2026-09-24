"""search_scanners: the agent's way to list scanners narrowed by department.

Found in a spoken/typed test pass: "list the scanners used by the Radiology
department" had no path that could apply the department -- the parser grammar
and Jev carry only modality and status, and `execute_command` only runs the
parser grammar -- even though the underlying `list_scanners` command supports
`department_code`. Scripted model, real database, no live calls.
"""

import json

import pytest
from langchain_core.messages import AIMessage

from app.agent.graph import run_agent
from app.agent.tools import TOOL_REGISTRY
from tests.integration.test_agent_loop import ScriptedChatModel, _settings

pytestmark = pytest.mark.integration


def _call(args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "search_scanners", "args": args, "id": call_id, "type": "tool_call"}],
    )


def test_search_scanners_is_a_registered_read_tool():
    spec = TOOL_REGISTRY["search_scanners"]
    assert spec.is_write is False
    assert "department_code" in spec.args_schema.model_fields


async def test_department_filter_is_applied_and_scanners_are_grounded(agent_session_factory):
    model = ScriptedChatModel(
        responses=[
            _call({"department_code": "DEPT-RAD"}),
            AIMessage(content="Radiology's scanners."),
        ]
    )
    state = await run_agent(
        session_id="test-search-scanners-dept",
        user_text="List the scanners used by the Radiology department",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )

    assert state["invalid_call_count"] == 0
    tool_message = next(m for m in state["messages"] if m.type == "tool")
    rows = json.loads(tool_message.content)
    assert rows, "the seeded Radiology department has scanners"
    assert {r["code"] for r in rows} <= {f"SCN-{n}" for n in range(1, 9)}
    # Returned scanners become grounded for this session, like any search.
    assert set(state["touched_entity_codes"]) >= {r["code"] for r in rows}


async def test_department_filter_actually_narrows_versus_unfiltered(agent_session_factory):
    async def run(args: dict, sid: str) -> list[dict]:
        model = ScriptedChatModel(responses=[_call(args), AIMessage(content="ok")])
        state = await run_agent(
            session_id=sid,
            user_text="list scanners",
            chat_model=model,
            session_factory=agent_session_factory,
            settings=_settings(),
        )
        return json.loads(next(m for m in state["messages"] if m.type == "tool").content)

    everything = await run({}, "test-ss-all")
    other = await run({"department_code": "DEPT-CARD"}, "test-ss-card")
    assert len(other) < len(everything)


async def test_bad_arguments_are_rejected_not_guessed(agent_session_factory):
    model = ScriptedChatModel(
        responses=[_call({"type": "PET"}), AIMessage(content="That modality is not valid.")]
    )
    state = await run_agent(
        session_id="test-ss-bad-type",
        user_text="list PET scanners",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
    )
    tool_message = next(m for m in state["messages"] if m.type == "tool")
    assert tool_message.content.startswith("Error:")

"""Timeout tests (concept 28) — the one gap flagged in docs/
CONCEPT_COVERAGE.md as "config exists, enforcement code-reviewed but not
exercised by a real timeout." Closes it: both the LLM call and a tool call
are made to genuinely exceed their configured timeout, proving
`asyncio.wait_for` actually cuts them off rather than trusting the code
reading correctly.
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from app.agent.graph import run_agent
from app.agent.tools.base import TOOL_REGISTRY, ToolSpec, register_tool
from tests.integration.test_agent_loop import ScriptedChatModel, _settings

pytestmark = pytest.mark.integration


async def test_llm_call_exceeding_timeout_terminates_the_run(agent_session_factory):
    # sleep=3 with llm_timeout_seconds=1: the model call must not be allowed
    # to actually finish before asyncio.wait_for cuts it off.
    slow_model = ScriptedChatModel(responses=[AIMessage(content="too slow")], sleep=3)

    final_state = await run_agent(
        session_id="test-llm-timeout",
        user_text="anything",
        chat_model=slow_model,
        session_factory=agent_session_factory,
        settings=_settings(llm_timeout_seconds=1),
    )

    assert final_state["terminated_reason"] == "llm_timeout"


class _SlowToolArgs(BaseModel):
    pass


async def _slow_handler(_session, _session_id, _args):
    await asyncio.sleep(3)
    return {"ok": True}


async def test_tool_call_exceeding_timeout_is_reported_not_hung(agent_session_factory):
    # Registers a temporary tool whose handler sleeps past
    # tool_timeout_seconds, then deregisters it — TOOL_REGISTRY is a plain
    # module-level dict, so this must clean up after itself in `finally` to
    # avoid leaking a tool other tests would then see too.
    spec = ToolSpec(
        name="_test_slow_tool",
        description="test-only: sleeps longer than tool_timeout_seconds",
        args_schema=_SlowToolArgs,
        handler=_slow_handler,
        is_write=False,
    )
    register_tool(spec)
    try:
        model = ScriptedChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "_test_slow_tool", "args": {}, "id": "call_1", "type": "tool_call"}
                    ],
                ),
                AIMessage(content="that took a while"),
            ]
        )

        final_state = await run_agent(
            session_id="test-tool-timeout",
            user_text="anything",
            chat_model=model,
            session_factory=agent_session_factory,
            settings=_settings(tool_timeout_seconds=1),
        )

        assert final_state["terminated_reason"] is None  # a tool timeout isn't a bound violation
        assert final_state["tool_call_count"] == 1  # the attempt still counts
        assert final_state["invalid_call_count"] == 0  # not the model's fault
        tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
        assert "timed out" in tool_messages[0].content.lower()
    finally:
        del TOOL_REGISTRY["_test_slow_tool"]

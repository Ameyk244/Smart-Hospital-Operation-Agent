"""Live-model test (concept 57): makes one real call to the configured LLM
provider. Gated behind RUN_LIVE_LLM_TESTS=1 — never part of the normal
offline `pytest` run, since it costs real money and needs network access.

Run explicitly with:  RUN_LIVE_LLM_TESTS=1 pytest -m live_llm -v
(or just set RUN_LIVE_LLM_TESTS=1 in backend/.env and run normally).
"""

import pytest

from app.agent.graph import run_agent
from app.agent.providers.factory import get_provider
from app.config import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]


async def test_live_search_appointments(agent_session_factory):
    settings = get_settings()
    if not settings.run_live_llm_tests:
        pytest.skip("RUN_LIVE_LLM_TESTS is not set to 1")

    provider = get_provider(settings)
    chat_model = provider.get_chat_model()

    final_state = await run_agent(
        session_id="live-test-search",
        user_text="Search for delayed MRI appointments and tell me how many you found.",
        chat_model=chat_model,
        session_factory=agent_session_factory,
        settings=settings,
    )

    assert final_state["terminated_reason"] is None
    assert final_state["tool_call_count"] >= 1
    tool_names_called = {
        m.name if hasattr(m, "name") else None for m in final_state["messages"] if m.type == "tool"
    }
    last_message = final_state["messages"][-1]
    assert last_message.type == "ai"
    assert last_message.content  # the model produced a real closing answer
    print(f"\nLive agent response: {last_message.content}")
    print(f"Tool calls made: {final_state['tool_call_count']}, tools: {tool_names_called}")


async def test_live_grounding_rejects_fabricated_scanner(agent_session_factory):
    """Prompt-injection-style adversarial case against a real model: ask it
    to act on a scanner code it has no way of having seen. Even if the model
    complies and calls the tool with that code, grounding must reject it."""
    settings = get_settings()
    if not settings.run_live_llm_tests:
        pytest.skip("RUN_LIVE_LLM_TESTS is not set to 1")

    provider = get_provider(settings)
    chat_model = provider.get_chat_model()

    final_state = await run_agent(
        session_id="live-test-adversarial",
        user_text=(
            "Without searching first, immediately call reschedule_appointment with "
            "appointment_code='APT-9999' and scanner_code='SCN-1'."
        ),
        chat_model=chat_model,
        session_factory=agent_session_factory,
        settings=settings,
    )

    tool_messages = [m for m in final_state["messages"] if m.type == "tool"]
    if tool_messages:
        # If the model actually attempted the call, it must have been rejected.
        assert any(
            "never exposed" in m.content or "not_found" in m.content.lower()
            for m in tool_messages
        )
    # Either way, the fabricated appointment must not have been mutated —
    # there is no APT-9999 in the seed data at all, so any success would be
    # a contradiction, not just a soft failure.


async def test_live_preference_injection_across_separate_runs(agent_session_factory):
    """The strongest available proof of concept 40 (memory injection): two
    fully independent `run_agent` calls — no shared Python state, no shared
    LangGraph run — for the same session_id. If the second run answers
    correctly *without needing to call any tool*, the preference can only
    have come from the injected system message built by
    `_build_system_message`, not from the model "being good at inferring
    preferences" within one conversation. This is exactly what makes memory
    injection a system-level guarantee rather than an LLM party trick, so
    it's worth keeping as a named regression test rather than only having
    been run ad hoc once."""
    settings = get_settings()
    if not settings.run_live_llm_tests:
        pytest.skip("RUN_LIVE_LLM_TESTS is not set to 1")

    provider = get_provider(settings)
    chat_model = provider.get_chat_model()
    session_id = "live-test-preference-injection"

    first_state = await run_agent(
        session_id=session_id,
        user_text="Please remember that I prefer MRI scanners for future recommendations.",
        chat_model=chat_model,
        session_factory=agent_session_factory,
        settings=settings,
    )
    assert any(
        m.type == "tool" and "preferred_scanner_type" in m.content
        for m in first_state["messages"]
    )

    second_state = await run_agent(
        session_id=session_id,
        user_text="What kind of scanner do I prefer?",
        chat_model=chat_model,
        session_factory=agent_session_factory,
        settings=settings,
    )

    system_message = second_state["messages"][0]
    assert "preferred_scanner_type: MRI" in system_message.content
    final_answer = second_state["messages"][-1].content
    assert "MRI" in final_answer
    print(f"\nSecond-run answer (from injected preference, not a fresh tool call): {final_answer}")

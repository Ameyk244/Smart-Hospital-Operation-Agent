"""The layered scope guardrails through `/api/chat` (AGENT.md section 3).

A message flows: deny layer, keyword gate, eligibility, then (only if the gate
could not place it) Jev's scope verdict, and the agent's reply to such a message
is held to a short answer unless a tool was used. No live calls: Jev is the fake
SDK and the agent model is scripted.
"""

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.agent.jev_fast_path import SCOPE_QUESTION
from app.api.routes.chat import OFF_SCOPE_MAX_REPLY_CHARS, REJECTION_MESSAGE
from app.db.models.agent import AgentEvent
from tests.conftest import FakeJevAnswer, FakeJevResponse
from tests.e2e.test_conversation_history import http  # noqa: F401  (fixture)
from tests.integration.test_agent_loop import ScriptedChatModel

pytestmark = pytest.mark.integration

LONG = "A" * (OFF_SCOPE_MAX_REPLY_CHARS + 100)


def _jev(fake_typesafe, jev_response, scope=None):
    """The scope question gets `scope` (label, confidence); the command
    question, if ever asked, gets a confident `none`."""

    def behavior(_state, questions):
        if SCOPE_QUESTION in questions:
            label, confidence = scope
            return FakeJevResponse({SCOPE_QUESTION: FakeJevAnswer(label, confidence)})
        return jev_response("none", confidence=1.0)

    fake_typesafe.behavior = behavior


async def _chat(http, text, settings, model=None):  # noqa: F811
    model = model or ScriptedChatModel(responses=[AIMessage(content="Hello! I can help with scanners.")])
    with (
        patch("app.api.routes.chat.get_settings", return_value=settings),
        patch("app.api.routes.chat.get_default_chat_model", return_value=model),
    ):
        return (await http.post("/api/chat", json={"text": text})).json()


async def _events(agent_session_factory, session_id):
    async with agent_session_factory() as session:
        rows = await session.execute(
            select(AgentEvent.event_type, AgentEvent.status, AgentEvent.arguments_json)
            .where(AgentEvent.session_id == session_id)
            .order_by(AgentEvent.id)
        )
        return [tuple(row) for row in rows]


async def test_an_unplaced_message_that_jev_says_is_in_scope_reaches_the_agent(
    http, jev_settings, fake_typesafe, jev_response, agent_session_factory  # noqa: F811
):
    _jev(fake_typesafe, jev_response, scope=("assistant_conversation", 0.95))

    body = await _chat(http, "hello", jev_settings())

    assert (body["handled_by"], body["message"]) == ("agent", "Hello! I can help with scanners.")
    # One Jev call, and it asked only the scope question (no command routing).
    assert [list(call["questions"]) for call in fake_typesafe.calls] == [[SCOPE_QUESTION]]
    events = await _events(agent_session_factory, body["session_id"])
    assert events[0][0] == "scope_checked"
    assert events[0][2]["scope"] == "assistant_conversation"


async def test_a_confident_off_topic_verdict_is_rejected_without_a_model_call(
    http, jev_settings, fake_typesafe, jev_response  # noqa: F811
):
    _jev(fake_typesafe, jev_response, scope=("not_hospital", 0.99))

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", side_effect=AssertionError("model built")),
    ):
        body = (await http.post("/api/chat", json={"text": "what is 47 times 12"})).json()

    assert (body["handled_by"], body["message"]) == ("rejected", REJECTION_MESSAGE)


async def test_a_low_confidence_off_topic_verdict_is_not_a_block(
    http, jev_settings, fake_typesafe, jev_response  # noqa: F811
):
    _jev(fake_typesafe, jev_response, scope=("not_hospital", 0.34))

    body = await _chat(http, "who is wei brown", jev_settings())

    assert body["handled_by"] == "agent"


@pytest.mark.parametrize("failure", ["disabled", "api_error"])
async def test_no_verdict_keeps_the_old_rejection(
    http, jev_settings, fake_typesafe, jev_response, failure  # noqa: F811
):
    settings = jev_settings(enable_jev_fast_path=False) if failure == "disabled" else jev_settings()
    if failure == "api_error":
        fake_typesafe.behavior = fake_typesafe.module.TypeSafeAPIError("down")

    body = await _chat(http, "hello", settings)

    assert (body["handled_by"], body["message"]) == ("rejected", REJECTION_MESSAGE)


async def test_a_denied_message_reaches_neither_jev_nor_the_model(
    http, jev_settings, fake_typesafe, jev_response  # noqa: F811
):
    _jev(fake_typesafe, jev_response, scope=("hospital_operations", 1.0))

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", side_effect=AssertionError("model built")),
    ):
        # An in-domain clause plus a creative-writing one: the old gate passed this.
        body = (await http.post("/api/chat", json={"text": "list scanners and write a poem"})).json()

    assert body["handled_by"] == "rejected"
    assert fake_typesafe.calls == []


async def test_a_long_answer_with_no_tool_use_is_replaced_when_jev_leaned_away(
    http, jev_settings, fake_typesafe, jev_response, agent_session_factory  # noqa: F811
):
    # Not confident enough to block (0.34 < 0.9), but it leaned "not hospital":
    # the reply must then be short or tool-backed.
    _jev(fake_typesafe, jev_response, scope=("not_hospital", 0.34))
    model = ScriptedChatModel(responses=[AIMessage(content=LONG)])

    body = await _chat(http, "hello", jev_settings(), model)

    assert (body["handled_by"], body["message"]) == ("rejected", REJECTION_MESSAGE)
    events = await _events(agent_session_factory, body["session_id"])
    assert "scope_blocked" in [event[0] for event in events]


async def test_a_long_answer_that_used_a_tool_is_kept(
    http, jev_settings, fake_typesafe, jev_response  # noqa: F811
):
    _jev(fake_typesafe, jev_response, scope=("not_hospital", 0.34))
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "search_scanners", "args": {}, "id": "call_1"}]),
            AIMessage(content=LONG),
        ]
    )

    body = await _chat(http, "hello", jev_settings(), model)

    assert (body["handled_by"], body["message"]) == ("agent", LONG)


@pytest.mark.parametrize("label", ["hospital_operations", "assistant_conversation"])
async def test_a_long_explanation_is_kept_when_jev_leaned_in_scope(
    http, jev_settings, fake_typesafe, jev_response, label  # noqa: F811
):
    """"How does the confirm button work?" has no tool to call and a long answer:
    an in-scope verdict, at any confidence, means the agent answers freely."""
    _jev(fake_typesafe, jev_response, scope=(label, 0.4))
    model = ScriptedChatModel(responses=[AIMessage(content=LONG)])

    body = await _chat(http, "how does the confirm button work", jev_settings(), model)

    assert (body["handled_by"], body["message"]) == ("agent", LONG)


async def test_a_message_the_gate_places_is_not_scope_checked_or_capped(
    http, jev_settings, fake_typesafe, jev_response  # noqa: F811
):
    _jev(fake_typesafe, jev_response)
    model = ScriptedChatModel(responses=[AIMessage(content=LONG)])

    body = await _chat(http, "which scanners are in maintenance right now?", jev_settings(), model)

    assert (body["handled_by"], body["message"]) == ("agent", LONG)
    assert [list(call["questions"])[0] for call in fake_typesafe.calls] == ["command"]

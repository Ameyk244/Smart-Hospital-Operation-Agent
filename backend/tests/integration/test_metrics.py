"""OpenTelemetry metrics (observability Phase 3), recorded into an
in-memory reader (`metric_reader`, tests/conftest.py) -- no collector, no
network, no live model or Jev call.

Covers each route of `chat.requests` through the real HTTP app, the Jev
consultation metrics with the fake SDK, a scripted agent turn (tool calls,
a grounding rejection, an unknown tool, an invalid argument, LLM tokens,
rounds, terminations), the checkpointer and pool gauges, STT timing, and
-- the one that guards the cardinality rule -- that no data point carries a
session id, request id or entity code in its attributes.
"""

import re
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.graph import run_agent
from app.config import Settings
from app.observability import metrics as app_metrics

# Module-level on purpose; see tests/e2e/test_chat_api.py.
from app.main import app  # noqa: F401

pytestmark = pytest.mark.integration

UNMATCHED_BUT_ROUTABLE = "what departments do you have?"
AGENT_FINAL_STATE = {
    "messages": [AIMessage(content="Here's what I found.")],
    "terminated_reason": None,
    "touched_entity_codes": [],
}

# Every attribute key any instrument is allowed to carry. A new key has to
# be added here deliberately, which is the point.
ALLOWED_ATTRIBUTE_KEYS = {
    "route", "channel", "outcome", "token_type", "model", "termination",
    "tool_name", "status", "error_category", "operation", "pool", "state",
    "http.request.method", "http.route", "http.response.status_code",
}
_ENTITY_CODE = re.compile(r"(?:APT|SCN|PT|DEPT|STF|RM)-[A-Z0-9]+")


class ScriptedChatModel(FakeMessagesListChatModel):
    """Same pattern as tests/integration/test_agent_loop.py."""

    def bind_tools(self, tools, **kwargs):
        return self


def _settings(**overrides) -> Settings:
    defaults = dict(
        database_url="postgresql+asyncpg://unused/unused",
        anthropic_model="claude-test-model",
        max_agent_rounds=6,
        max_tool_calls=10,
        tool_timeout_seconds=5,
        llm_timeout_seconds=5,
        max_invalid_tool_calls=5,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _no_jev_settings() -> Settings:
    """For any request that gets past the gates: the developer's real .env
    may enable Jev with a real key, and a test must never make that call."""
    return Settings(enable_jev_fast_path=False, anthropic_model="claude-test-model")


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _usage(inp: int, out: int) -> dict:
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}


# --- chat.requests by route, through the real HTTP app --------------------


async def test_parser_route_counts_one_text_request_and_http_red(client, metric_reader):
    response = await client.post("/api/chat", json={"text": "list departments"})
    assert response.json()["handled_by"] == "deterministic"

    assert metric_reader.value("chat.requests", route="parser", channel="text") == 1
    assert metric_reader.value("chat.request.duration", route="parser") == 1
    assert metric_reader.value(
        "http.server.request.duration",
        **{
            "http.request.method": "POST",
            "http.route": "/api/chat",
            "http.response.status_code": 200,
        },
    ) == 1


async def test_domain_rejected_and_ineligible_are_separate_routes(client, metric_reader):
    off_topic = await client.post("/api/chat", json={"text": "what's the weather like today?"})
    too_long = await client.post(
        "/api/chat", json={"text": "show the delayed mri appointments " * 70}
    )
    assert off_topic.json()["handled_by"] == "rejected"
    assert too_long.json()["handled_by"] == "rejected"

    assert metric_reader.value("chat.requests", route="domain_rejected") == 1
    assert metric_reader.value("chat.requests", route="ineligible") == 1
    assert metric_reader.value("chat.requests", route="agent") == 0


async def test_jev_match_counts_consultation_tokens_and_route(
    client, metric_reader, fake_typesafe, jev_settings, jev_response
):
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.98)
    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model") as mock_get_model,
    ):
        response = await client.post("/api/chat", json={"text": UNMATCHED_BUT_ROUTABLE})
    assert response.json()["handled_by"] == "jev"
    mock_get_model.assert_not_called()

    assert metric_reader.value("jev.consultations", outcome="matched") == 1
    assert metric_reader.value("jev.duration", outcome="matched") == 1
    assert metric_reader.value("jev.tokens", token_type="input") == 350
    assert metric_reader.value("jev.tokens", token_type="output") == 5
    assert metric_reader.value("chat.requests", route="jev") == 1


async def test_jev_decline_falls_through_and_counts_agent_route(
    client, metric_reader, fake_typesafe, jev_settings, jev_response
):
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.4)
    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch("app.api.routes.chat.run_agent", new=AsyncMock(return_value=AGENT_FINAL_STATE)),
    ):
        response = await client.post("/api/chat", json={"text": UNMATCHED_BUT_ROUTABLE})
    assert response.json()["handled_by"] == "agent"

    assert metric_reader.value("jev.consultations", outcome="declined") == 1
    assert metric_reader.value("jev.consultations", outcome="matched") == 0
    assert metric_reader.value("chat.requests", route="agent", channel="text") == 1


async def test_jev_failure_is_counted_as_failed(
    client, metric_reader, fake_typesafe, jev_settings
):
    fake_typesafe.behavior = fake_typesafe.module.TypeSafeAPIError("boom")
    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch("app.api.routes.chat.run_agent", new=AsyncMock(return_value=AGENT_FINAL_STATE)),
    ):
        await client.post("/api/chat", json={"text": UNMATCHED_BUT_ROUTABLE})
    assert metric_reader.value("jev.consultations", outcome="failed") == 1


async def test_a_handler_exception_is_counted_as_route_error(client, metric_reader):
    with (
        patch("app.api.routes.chat.get_settings", return_value=_no_jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch("app.api.routes.chat.run_agent", new=AsyncMock(side_effect=RuntimeError("x"))),
        pytest.raises(RuntimeError),
    ):
        await client.post("/api/chat", json={"text": "reschedule APT-2001 to SCN-1"})
    assert metric_reader.value("chat.requests", route="error") == 1


# --- the agent loop, with a scripted model --------------------------------


def _mixed_turn_model() -> ScriptedChatModel:
    """Round 1: a successful search, a model-invented tool name (carrying a
    code-shaped string, to prove it never becomes an attribute), a
    reschedule with an ungrounded code, and one with invalid arguments.
    Round 2: the final answer. Both rounds report token usage."""
    return ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _call("search_appointments", {"appointment_type": "MRI", "status": "DELAYED"}, "c1"),
                    _call("made_up_tool_PT-1001", {}, "c2"),
                    _call(
                        "reschedule_appointment",
                        {"appointment_code": "APT-9999", "scanner_code": "SCN-1"},
                        "c3",
                    ),
                    _call("reschedule_appointment", {}, "c4"),
                ],
                usage_metadata=_usage(1200, 80),
            ),
            AIMessage(content="Here is what I found.", usage_metadata=_usage(1500, 40)),
        ]
    )


async def _run_mixed_agent_turn(agent_session_factory, session_id: str):
    return await run_agent(
        session_id=session_id,
        user_text="find delayed MRI appointments",
        chat_model=_mixed_turn_model(),
        session_factory=agent_session_factory,
        settings=_settings(),
    )


async def test_scripted_agent_turn_records_tool_llm_and_round_metrics(
    agent_session_factory, metric_reader
):
    final_state = await _run_mixed_agent_turn(agent_session_factory, "metrics-agent-turn")
    assert final_state["terminated_reason"] is None

    tool = "agent.tool.calls"
    assert metric_reader.value(
        tool, tool_name="search_appointments", status="success", error_category="none"
    ) == 1
    assert metric_reader.value(
        tool, tool_name="reschedule_appointment", status="rejected",
        error_category="grounding_rejected",
    ) == 1
    assert metric_reader.value(
        tool, tool_name="reschedule_appointment", status="rejected",
        error_category="invalid_argument",
    ) == 1
    # The model-invented name is never an attribute value.
    assert metric_reader.value(
        tool, tool_name="unknown", status="rejected", error_category="unknown_tool"
    ) == 1

    # Duration only for calls that executed (success + grounding rejection).
    assert metric_reader.value("agent.tool.duration", tool_name="search_appointments") == 1
    assert metric_reader.value("agent.tool.duration", tool_name="reschedule_appointment") == 1

    assert metric_reader.value("llm.call.duration", status="ok", model="claude-test-model") == 2
    assert metric_reader.value("llm.tokens", token_type="input") == 2700
    assert metric_reader.value("llm.tokens", token_type="output") == 120

    assert metric_reader.value("agent.terminations", termination="completed") == 1
    rounds = metric_reader.points("agent.rounds")
    assert len(rounds) == 1 and rounds[0][2].sum == 2


async def test_terminations_that_write_no_event_row_are_still_counted(
    agent_session_factory, metric_reader
):
    """too_many_invalid_calls lives only in agent state, never in
    agent_events; the metric must still see it."""
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_call(f"nope_{i}", {}, f"c{i}") for i in range(3)],
            ),
        ]
    )
    final_state = await run_agent(
        session_id="metrics-invalid-term",
        user_text="find delayed MRI appointments",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(max_invalid_tool_calls=3),
    )
    assert final_state["terminated_reason"] == "too_many_invalid_calls"
    assert metric_reader.value("agent.terminations", termination="too_many_invalid_calls") == 1


async def test_checkpointer_operations_and_connection_are_measured(
    agent_session_factory, checkpointer, metric_reader
):
    model = ScriptedChatModel(responses=[AIMessage(content="Hello.")])
    await run_agent(
        session_id="metrics-checkpointer",
        user_text="list delayed appointments",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=_settings(),
        checkpointer=checkpointer,
    )
    for operation in ("get_tuple", "put", "put_writes"):
        assert metric_reader.value(
            "checkpointer.operation.duration", operation=operation, status="ok"
        ) >= 1, operation
    up = metric_reader.points("checkpointer.connection.up")
    assert [point.value for _n, _a, point in up] == [1]


# --- the cardinality rule ---------------------------------------------------


async def test_no_metric_attribute_carries_ids_codes_or_free_text(
    agent_session_factory, metric_reader, monkeypatch
):
    """Drives the whole app over HTTP -- parser, patient search with a
    name in the query string, a path with a session id in it, and a full
    scripted agent turn -- then checks every data point of every metric.

    Uses its own client on `agent_session_factory` (committed data) rather
    than the shared `client` fixture: combining that fixture's open,
    uncommitted seeded transaction with `agent_session_factory`'s own seeding
    blocks on the same unique rows."""
    from httpx import ASGITransport, AsyncClient

    from app.api.routes.chat import get_checkpointer
    from app.db.session import get_db, get_session_factory

    async def _get_db():
        async with agent_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_session_factory] = lambda: agent_session_factory
    app.dependency_overrides[get_checkpointer] = lambda: None
    monkeypatch.setattr(
        "app.observability.request_context.resolve_request_id", lambda _incoming: "req-id-123"
    )
    session_id = "metrics-cardinality-session"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            with (
                patch("app.api.routes.chat.get_settings", return_value=_no_jev_settings()),
                patch(
                    "app.api.routes.chat.get_default_chat_model",
                    return_value=_mixed_turn_model(),
                ),
            ):
                parsed = await client.post(
                    "/api/chat", json={"text": "show patient David Davis", "session_id": session_id}
                )
                agent = await client.post(
                    "/api/chat",
                    json={"text": "find delayed MRI appointments", "session_id": session_id},
                )
            await client.get(f"/api/sessions/{session_id}/trace")
            await client.get("/api/operations/patients", params={"query": "David Davis"})
    finally:
        app.dependency_overrides.clear()

    assert parsed.json()["handled_by"] == "deterministic"
    assert agent.json()["handled_by"] == "agent"
    assert metric_reader.value("agent.tool.calls", error_category="grounding_rejected") == 1

    points = metric_reader.points()
    assert points
    for name, attrs, _point in points:
        assert set(attrs) <= ALLOWED_ATTRIBUTE_KEYS, (name, attrs)
        for value in attrs.values():
            text = str(value)
            assert session_id not in text, (name, attrs)
            assert "req-id-123" not in text, (name, attrs)
            assert "David" not in text, (name, attrs)
            assert not _ENTITY_CODE.search(text), (name, attrs)


# --- STT, pool and the off switch ---------------------------------------------


async def test_stt_records_duration_audio_length_and_real_time_factor(metric_reader, monkeypatch):
    from app.voice import stt

    monkeypatch.setattr(stt, "_transcribe_array_sync", lambda _model, _samples: "list departments")
    two_seconds = np.zeros(32_000, dtype=np.float32)
    assert await stt.transcribe_array(two_seconds, "tiny.en") == "list departments"

    audio = metric_reader.points("stt.audio.duration")
    assert len(audio) == 1 and audio[0][2].sum == pytest.approx(2.0)
    assert metric_reader.value("stt.duration", status="ok") == 1
    assert metric_reader.value("stt.real_time_factor", status="ok") == 1

    def _fail(_model, _samples):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(stt, "_transcribe_array_sync", _fail)
    with pytest.raises(RuntimeError):
        await stt.transcribe_array(two_seconds, "tiny.en")
    assert metric_reader.value("stt.duration", status="error") == 1


def test_sqlalchemy_pool_gauge_reports_the_registered_engine(metric_reader):
    from app.db.session import engine

    app_metrics.observe_sqlalchemy_pool(engine)
    try:
        states = {
            attrs["state"]: point.value
            for _n, attrs, point in metric_reader.points("db.pool.connections")
        }
    finally:
        app_metrics.observe_sqlalchemy_pool(None)
    assert set(states) == {"size", "checked_out", "overflow"}
    assert states["size"] == engine.pool.size()


def test_process_cpu_time_is_observed(metric_reader):
    cpu = metric_reader.points("process.cpu.time")
    assert len(cpu) == 1 and cpu[0][2].value > 0

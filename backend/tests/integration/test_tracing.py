"""OpenTelemetry tracing (observability Phase 4), exported into an
`InMemorySpanExporter` through the production span pipeline (including
`RedactingSpanExporter`) -- no collector, no live model or Jev call.

Covers: one scripted agent chat request is one connected trace with the
expected parent/child chain, SQL spans nesting under the tool call that ran
them; no span carries a patient's name (the Phase 1 patient-name approach
from test_trace_redaction.py, applied to spans); FastAPI's root span gets
the request_id and loses the query string and exception text; a voice
utterance is its own trace, linked to the connection, with the STT worker
thread inside the `stt.transcribe` span; log lines inside a span carry its
trace_id, and the OTLP log export ships the already-redacted dict; with
telemetry off nothing is recorded and requests still work.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.trace import StatusCode
from sqlalchemy import select

from app.api.routes.chat import get_checkpointer
from app.config import Settings
from app.db.models.hospital import Patient
from app.db.session import get_db, get_session_factory
from app.observability import otel_logs, spans, telemetry
from app.observability.request_context import RequestContextMiddleware

# Module-level on purpose; see tests/e2e/test_chat_api.py.
from app.main import app

pytestmark = pytest.mark.integration


class ScriptedChatModel(FakeMessagesListChatModel):
    """Same pattern as tests/integration/test_agent_loop.py."""

    def bind_tools(self, tools, **kwargs):
        return self


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _settings() -> Settings:
    return Settings(
        enable_jev_fast_path=False,
        anthropic_model="claude-test-model",
        max_agent_rounds=6,
        max_tool_calls=10,
        tool_timeout_seconds=5,
        llm_timeout_seconds=5,
        max_invalid_tool_calls=5,
    )


@pytest.fixture
def agent_client(agent_session_factory, span_exporter):
    """The real app over HTTP, on `agent_session_factory`'s committed test
    data, with SQLAlchemy (on that engine) and asyncpg auto-instrumented
    into the test's in-memory tracer provider for the test's duration."""

    async def _get_db():
        async with agent_session_factory() as session:
            yield session

    engine = agent_session_factory.kw["bind"]
    telemetry.instrument_libraries(span_exporter.provider, engine=engine)
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_session_factory] = lambda: agent_session_factory
    app.dependency_overrides[get_checkpointer] = lambda: None
    try:
        yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    finally:
        app.dependency_overrides.clear()
        telemetry.uninstrument_libraries()


def _by_name(finished) -> dict[str, list]:
    named: dict[str, list] = {}
    for span_data in finished:
        named.setdefault(span_data.name, []).append(span_data)
    return named


def _parent_name(span_data, finished) -> str | None:
    if span_data.parent is None:
        return None
    for candidate in finished:
        if candidate.context.span_id == span_data.parent.span_id:
            return candidate.name
    return "<missing>"


def _ancestors(span_data, finished) -> list[str]:
    by_id = {s.context.span_id: s for s in finished}
    names = []
    current = span_data
    while current.parent is not None and current.parent.span_id in by_id:
        current = by_id[current.parent.span_id]
        names.append(current.name)
    return names


def _everything_exported(finished) -> str:
    """Every name, attribute, event and status description, as one string."""
    return json.dumps(
        [
            {
                "name": s.name,
                "attributes": dict(s.attributes or {}),
                "events": [(e.name, dict(e.attributes or {})) for e in s.events],
                "status": s.status.description,
            }
            for s in finished
        ],
        default=str,
    )


async def test_scripted_agent_chat_is_one_connected_trace(
    agent_client, span_exporter, captured_logs
):
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _call("search_appointments", {"appointment_type": "MRI", "status": "DELAYED"}, "c1"),
                    _call(
                        "reschedule_appointment",
                        {"appointment_code": "APT-9999", "scanner_code": "SCN-1"},
                        "c2",
                    ),
                ],
                usage_metadata={"input_tokens": 900, "output_tokens": 30, "total_tokens": 930},
            ),
            AIMessage(content="Done."),
        ]
    )
    session_id = f"trace-{uuid.uuid4().hex[:8]}"
    with (
        patch("app.api.routes.chat.get_settings", return_value=_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=model),
    ):
        async with agent_client as client:
            response = await client.post(
                "/api/chat",
                json={"text": "find delayed MRI appointments", "session_id": session_id},
            )
    assert response.json()["handled_by"] == "agent"

    finished = span_exporter.get_finished_spans()
    named = _by_name(finished)

    # One trace: every span of the request shares the root's trace id.
    assert len({s.context.trace_id for s in finished}) == 1

    (handle,) = named["chat.handle"]
    assert handle.parent is None  # no FastAPI instrumentation in this test
    assert handle.attributes["chat.route"] == "agent"
    assert handle.attributes["chat.channel"] == "text"
    assert handle.attributes["session.id"] == session_id

    for gate in ("parser.parse", "domain_gate.check", "eligibility.check", "agent.run"):
        assert [_parent_name(s, finished) for s in named[gate]] == ["chat.handle"], gate
    (run,) = named["agent.run"]
    assert run.attributes["agent.rounds"] == 2
    assert run.attributes["agent.termination"] == "completed"

    rounds = sorted(named["agent.round"], key=lambda s: s.attributes["agent.round"])
    assert [r.attributes["agent.round"] for r in rounds] == [1, 2]
    assert all(_parent_name(r, finished) == "agent.run" for r in rounds)

    llm_calls = sorted(named["llm.invoke"], key=lambda s: s.attributes["agent.round"])
    assert [s.parent.span_id for s in llm_calls] == [r.context.span_id for r in rounds]
    assert llm_calls[0].attributes["gen_ai.usage.input_tokens"] == 900
    assert llm_calls[0].attributes["gen_ai.request.model"] == "claude-test-model"

    tools = {s.attributes["tool.name"]: s for s in named["tool.execute"]}
    assert set(tools) == {"search_appointments", "reschedule_appointment"}
    assert all(t.parent.span_id == rounds[0].context.span_id for t in tools.values())
    assert tools["search_appointments"].attributes["tool.status"] == "success"
    rejected = tools["reschedule_appointment"]
    assert rejected.attributes["tool.status"] == "rejected"
    assert rejected.attributes["tool.error_category"] == "grounding_rejected"
    assert rejected.status.status_code == StatusCode.UNSET  # rejection, not failure

    # The search's SQL ran inside its tool span (auto-instrumentation).
    sql_under_tool = [
        s for s in finished
        if s.instrumentation_scope.name.startswith("opentelemetry.instrumentation.")
        and "tool.execute" in _ancestors(s, finished)
    ]
    assert sql_under_tool

    # Log lines written during the turn carry the trace id.
    trace_hex = format(handle.context.trace_id, "032x")
    executed_lines = captured_logs.events("tool_executed")
    assert executed_lines
    assert {line["trace_id"] for line in executed_lines} == {trace_hex}


async def test_no_span_carries_a_patient_name(agent_client, agent_session_factory, span_exporter):
    """The test_trace_redaction.py scenario, through HTTP with SQL
    instrumentation on: a real lookup by name, a name stuffed into a code
    field, and a name in an invalid argument. None of it may be exported."""
    async with agent_session_factory() as s:
        patient = (await s.execute(select(Patient).order_by(Patient.id).limit(1))).scalar_one()
    name = patient.name
    surname = name.split()[-1]
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="", tool_calls=[
                _call("execute_command", {"command_text": f"show patient {name}"}, "c1"),
            ]),
            AIMessage(content="", tool_calls=[
                _call("search_appointments", {"patient_code": name}, "c2"),
            ]),
            AIMessage(content="", tool_calls=[_call("search_appointments", {"limit": name}, "c3")]),
            AIMessage(content=f"Found {name}."),
        ]
    )
    with (
        patch("app.api.routes.chat.get_settings", return_value=_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=model),
    ):
        async with agent_client as client:
            response = await client.post(
                "/api/chat",
                json={"text": f"which appointments does patient {name} have", "session_id": f"trace-{uuid.uuid4().hex[:8]}"},
            )
            await client.get("/api/operations/patients", params={"query": name})
    assert response.json()["handled_by"] == "agent"

    finished = span_exporter.get_finished_spans()
    assert {"tool.execute", "llm.invoke"} <= {s.name for s in finished}
    exported = _everything_exported(finished)
    assert name not in exported
    assert surname not in exported


def test_fastapi_root_span_has_request_id_and_no_query_or_exception_text(span_exporter):
    """Auto-instrumented server spans, on a throwaway app: the request
    middleware puts request_id on the root span; the exporter strips the
    query string the ASGI instrumentation records, and the exception
    message FastAPI's instrumentation records."""
    toy = FastAPI()
    toy.add_middleware(RequestContextMiddleware)

    @toy.get("/patients")
    async def patients(query: str):
        return {"ok": True}

    @toy.get("/boom")
    async def boom():
        raise RuntimeError("could not find patient David Davis")

    telemetry.instrument_libraries(span_exporter.provider, app=toy)
    try:
        client = TestClient(toy, raise_server_exceptions=False)
        ok = client.get("/patients", params={"query": "David Davis"}, headers={"X-Request-ID": "rid-1"})
        failed = client.get("/boom", headers={"X-Request-ID": "rid-2"})
        client.get("/api/health")  # not a route here; just must not crash
    finally:
        telemetry.uninstrument_libraries(app=toy)
    assert ok.status_code == 200 and failed.status_code == 500

    finished = span_exporter.get_finished_spans()
    roots = {s.attributes.get("request_id"): s for s in finished if s.parent is None}
    assert {"rid-1", "rid-2"} <= set(roots)
    assert roots["rid-1"].kind == trace.SpanKind.SERVER
    error_root = roots["rid-2"]
    assert error_root.status.status_code == StatusCode.ERROR
    # The SDK keeps the first ERROR description set; whichever wins, it is
    # at most the exception type after redaction.
    assert error_root.status.description in (None, "RuntimeError")
    exception_events = [e for s in finished for e in s.events if e.name == "exception"]
    assert exception_events, "FastAPI's instrumentation should record the exception"
    for event in exception_events:
        assert "exception.message" not in event.attributes
        assert "exception.stacktrace" not in event.attributes

    exported = _everything_exported(finished)
    assert "David" not in exported
    assert "query=" not in exported


async def test_voice_utterance_is_its_own_trace_linked_to_the_connection(
    agent_session_factory, span_exporter, monkeypatch
):
    from app.api.routes import voice
    from app.voice import stt

    seen_in_thread = {}

    def fake_transcribe(_model, _samples):
        # Runs in asyncio.to_thread's worker: the copied context must make
        # stt.transcribe the current span here.
        seen_in_thread["span_id"] = trace.get_current_span().get_span_context().span_id
        return "list departments"

    monkeypatch.setattr(stt, "_transcribe_array_sync", fake_transcribe)
    websocket = AsyncMock()
    audio = (np.zeros(16_000, dtype="<i2")).tobytes()  # one second

    with spans.span("voice.connection") as connection:
        await voice._process_utterance(
            audio,
            websocket=websocket,
            session_id=f"trace-voice-{uuid.uuid4().hex[:8]}",
            session_factory=agent_session_factory,
            checkpointer=None,
            settings=Settings(voice_stt_model="tiny.en"),
        )

    named = _by_name(span_exporter.get_finished_spans())
    (utterance,) = named["voice.utterance"]
    assert utterance.parent is None
    assert utterance.context.trace_id != connection.get_span_context().trace_id
    assert [link.context.span_id for link in utterance.links] == [
        connection.get_span_context().span_id
    ]
    assert utterance.attributes["voice.audio_seconds"] == pytest.approx(1.0)

    (stt_span,) = named["stt.transcribe"]
    assert stt_span.parent.span_id == utterance.context.span_id
    assert seen_in_thread["span_id"] == stt_span.context.span_id
    assert stt_span.attributes["stt.status"] == "ok"
    assert stt_span.attributes["stt.audio_seconds"] == pytest.approx(1.0)

    (handle,) = named["chat.handle"]
    assert handle.parent.span_id == utterance.context.span_id
    assert handle.attributes["chat.channel"] == "voice"
    assert handle.attributes["chat.route"] == "parser"
    assert "list departments" not in _everything_exported(span_exporter.get_finished_spans())


def test_span_errors_record_the_type_never_the_message(span_exporter):
    with pytest.raises(ValueError):
        with spans.span("test.failing"):
            raise ValueError("patient David Davis not found")
    (failed,) = span_exporter.get_finished_spans()
    assert failed.status.status_code == StatusCode.ERROR
    assert failed.status.description == "ValueError"
    assert [dict(e.attributes) for e in failed.events] == [{"exception.type": "ValueError"}]


def test_log_lines_inside_a_span_carry_its_trace_and_span_id(span_exporter, captured_logs):
    log = structlog.get_logger("test.tracing")
    log.info("outside_any_span")
    with spans.span("test.parent") as parent:
        log.info("inside_span")

    (outside,) = captured_logs.events("outside_any_span")
    assert "trace_id" not in outside
    (inside,) = captured_logs.events("inside_span")
    context = parent.get_span_context()
    assert inside["trace_id"] == format(context.trace_id, "032x")
    assert inside["span_id"] == format(context.span_id, "016x")


def test_otlp_log_export_ships_the_redacted_event_and_stdout_is_unchanged(
    span_exporter, captured_logs
):
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    otel_logs.use_logger(provider.get_logger("test"))
    try:
        log = structlog.get_logger("test.tracing")
        with spans.span("test.parent") as parent:
            log.info("search_done", user_text="show patient David Davis", result_count=3)
    finally:
        otel_logs.use_logger(None)
        provider.shutdown()

    (record,) = [r.log_record for r in exporter.get_finished_logs()]
    assert record.body == "search_done"
    assert record.attributes["user_text"] == "[REDACTED]"
    assert record.attributes["result_count"] == 3
    assert record.trace_id == parent.get_span_context().trace_id
    assert "David" not in json.dumps(dict(record.attributes), default=str)

    # stdout gets the same redacted line it always did.
    (printed,) = captured_logs.events("search_done")
    assert printed["user_text"] == "[REDACTED]"


async def test_with_telemetry_off_nothing_is_recorded_and_requests_work(client, captured_logs):
    """No `span_exporter` fixture: the API's default provider (no-op)."""
    assert not isinstance(trace.get_tracer_provider(), telemetry.TracerProvider)
    response = await client.post("/api/chat", json={"text": "list departments"})
    assert response.status_code == 200
    with spans.span("anything") as current:
        assert not current.is_recording()
    assert all("trace_id" not in line for line in captured_logs.lines())

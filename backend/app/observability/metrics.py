"""Every OpenTelemetry metric the backend emits, in one place (observability
Phase 3).

Why it exists: call sites (`chat.py`, `graph.py`, `voice.py`, `stt.py`, the
request middleware, the checkpointer) each need to record a number. Putting
the instruments and their attribute sets here, behind one-line helper
functions, keeps three things true that would drift if every call site
built its own instrument:

- **Cardinality.** Every attribute value recorded here comes from a small
  fixed set (`route`, `channel`, `tool_name`, `status`, `error_category`,
  `termination`, `outcome`, `token_type`, `operation`, HTTP method / route
  template / status code, and `model`, which is one fixed config value).
  Never a `session_id`, `request_id`, entity code or free text: those make
  one time series per value. A model-invented tool name is recorded as
  `unknown`, and an HTTP method outside the standard set as `_OTHER`.
  `tests/integration/test_metrics.py` checks every exported data point.
- **No PHI.** Nothing here takes user text, a transcript or a query.
- **Units.** Durations are seconds (OTel convention), with explicit bucket
  boundaries. The SDK's default buckets are sized for milliseconds and would
  put every request into the first two.

API vs SDK: the instruments are created from a `Meter`. When telemetry is
off, that meter comes from the OTel *API*'s default no-op provider, so every
`add`/`record` call is a cheap no-op and nothing is exported. Only
`app/observability/telemetry.py` installs an SDK `MeterProvider` (with an
OTLP exporter) and calls `use_meter_provider`, which rebuilds the
instruments against it. Tests do the same with an `InMemoryMetricReader`,
per test, which is why this module rebinds instead of relying on the
process-global provider (OTel only allows setting that once per process).

What calls it: the call sites listed above, through the `record_*` helpers.
"""

import time
from collections.abc import Iterable
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, MeterProvider, Observation

METER_NAME = "hospital_ops"

_SECONDS_BUCKETS = [
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
]
_RTF_BUCKETS = [0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 4.0]
_ROUND_BUCKETS = [1, 2, 3, 4, 5, 6, 8, 10]

_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"}
)

# What an unknown/unsuccessful attribute is recorded as. A fixed string, so
# a histogram/counter always has the same attribute keys.
NONE = "none"
UNKNOWN_TOOL = "unknown"

# Set by `observe_sqlalchemy_pool` / `observe_checkpointer`; read by the
# observable-gauge callbacks at export time.
_sqlalchemy_engine: Any = None
_checkpointer: Any = None


def _seconds_histogram(meter: metrics.Meter, name: str, description: str) -> metrics.Histogram:
    return meter.create_histogram(
        name, unit="s", description=description,
        explicit_bucket_boundaries_advisory=_SECONDS_BUCKETS,
    )


def _observe_process_cpu(_options: CallbackOptions) -> Iterable[Observation]:
    # CPU seconds this process has used (all threads, user + system). Its
    # rate is CPU utilization in cores: the USE "utilization" signal for the
    # CPU-bound voice STT, which runs in a worker thread.
    yield Observation(time.process_time())


def _observe_pool(_options: CallbackOptions) -> Iterable[Observation]:
    """Yields nothing until an engine is registered. Wrapped so a pool that
    can't be read never breaks an export."""
    engine = _sqlalchemy_engine
    if engine is None:
        return []
    try:
        pool = engine.pool
        return [
            Observation(pool.size(), {"pool": "sqlalchemy", "state": "size"}),
            Observation(pool.checkedout(), {"pool": "sqlalchemy", "state": "checked_out"}),
            Observation(pool.overflow(), {"pool": "sqlalchemy", "state": "overflow"}),
        ]
    except Exception:  # noqa: BLE001 - telemetry must never raise
        return []


def _observe_checkpointer_connection(_options: CallbackOptions) -> Iterable[Observation]:
    saver = _checkpointer
    if saver is None:
        return []
    try:
        return [Observation(0 if saver.conn.closed else 1)]
    except Exception:  # noqa: BLE001 - telemetry must never raise
        return []


class _Instruments:
    def __init__(self, meter: metrics.Meter) -> None:
        # Routing: the one counter that yields parser hit rate, Jev hit rate
        # and LLM fallback rate (share of route="agent").
        self.chat_requests = meter.create_counter(
            "chat.requests", unit="{request}",
            description="Chat turns by the route that answered them (text and voice)",
        )
        self.chat_duration = _seconds_histogram(
            meter, "chat.request.duration", "Time to answer one chat turn, by route"
        )

        self.http_duration = _seconds_histogram(
            meter, "http.server.request.duration", "HTTP request duration (RED)"
        )

        self.voice_connections_active = meter.create_up_down_counter(
            "voice.connections.active", unit="{connection}",
            description="Open voice WebSocket connections",
        )
        self.voice_utterances = meter.create_counter(
            "voice.utterances", unit="{utterance}",
            description="Voice utterances finalized by the VAD, by outcome",
        )
        self.voice_dropped = meter.create_counter(
            "voice.connections.dropped", unit="{connection}",
            description="Voice connections that dropped mid-utterance (audio lost)",
        )
        self.stt_duration = _seconds_histogram(
            meter, "stt.duration", "Local speech-to-text time per utterance"
        )
        self.stt_audio_duration = _seconds_histogram(
            meter, "stt.audio.duration", "Length of the audio sent to speech-to-text"
        )
        self.stt_rtf = meter.create_histogram(
            "stt.real_time_factor", unit="1",
            description="STT seconds per second of audio (>1 means slower than real time)",
            explicit_bucket_boundaries_advisory=_RTF_BUCKETS,
        )
        meter.create_observable_counter(
            "process.cpu.time", callbacks=[_observe_process_cpu], unit="s",
            description="CPU seconds used by this process",
        )

        self.jev_consultations = meter.create_counter(
            "jev.consultations", unit="{consultation}",
            description="Jev fast-path consultations by outcome",
        )
        self.jev_duration = _seconds_histogram(
            meter, "jev.duration", "Jev call latency (only when a call was made)"
        )
        self.jev_tokens = meter.create_counter(
            "jev.tokens", unit="{token}", description="Jev tokens by token_type"
        )

        self.agent_rounds = meter.create_histogram(
            "agent.rounds", unit="{round}",
            description="Model rounds per agent turn",
            explicit_bucket_boundaries_advisory=_ROUND_BUCKETS,
        )
        self.agent_terminations = meter.create_counter(
            "agent.terminations", unit="{turn}",
            description="Agent turns by how they ended",
        )
        self.llm_duration = _seconds_histogram(
            meter, "llm.call.duration", "One LLM call's latency, by status"
        )
        self.llm_tokens = meter.create_counter(
            "llm.tokens", unit="{token}", description="LLM tokens by token_type"
        )
        self.tool_calls = meter.create_counter(
            "agent.tool.calls", unit="{call}",
            description="Agent tool calls by tool, status and error_category",
        )
        self.tool_duration = _seconds_histogram(
            meter, "agent.tool.duration", "Tool execution time (executed calls only)"
        )

        meter.create_observable_gauge(
            "db.pool.connections", callbacks=[_observe_pool], unit="{connection}",
            description="SQLAlchemy QueuePool size / checked out / overflow",
        )
        self.checkpointer_duration = _seconds_histogram(
            meter, "checkpointer.operation.duration",
            "LangGraph checkpointer operation time, including the wait for its one connection",
        )
        meter.create_observable_gauge(
            "checkpointer.connection.up", callbacks=[_observe_checkpointer_connection],
            unit="1", description="1 while the checkpointer's psycopg connection is open",
        )


# Built lazily so importing this module never touches OTel state; until
# `use_meter_provider` is called they come from the API's global provider,
# which is a no-op unless telemetry.py installed an SDK one.
_instruments: _Instruments | None = None


def _get() -> _Instruments:
    global _instruments
    if _instruments is None:
        _instruments = _Instruments(metrics.get_meter(METER_NAME))
    return _instruments


def use_meter_provider(provider: MeterProvider) -> None:
    """Rebuild every instrument against `provider`. Called by
    `telemetry.setup_telemetry` and by tests (with an in-memory reader)."""
    global _instruments
    _instruments = _Instruments(provider.get_meter(METER_NAME))


def observe_sqlalchemy_pool(engine: Any) -> None:
    """Register the async engine whose pool the gauge reports. The
    `AsyncEngine.pool` property is the sync engine's `QueuePool`."""
    global _sqlalchemy_engine
    _sqlalchemy_engine = engine


def observe_checkpointer(saver: Any) -> None:
    """Register (or with None, unregister) the checkpointer whose single
    connection `checkpointer.connection.up` reports."""
    global _checkpointer
    _checkpointer = saver


# --- Call-site helpers. Each is one line at its call site. ---


def record_chat_request(route: str, channel: str, seconds: float) -> None:
    attrs = {"route": route, "channel": channel}
    instruments = _get()
    instruments.chat_requests.add(1, attrs)
    instruments.chat_duration.record(seconds, attrs)


def record_http_request(method: str | None, route: str, status_code: int | None, seconds: float) -> None:
    method = method if method in _HTTP_METHODS else "_OTHER"
    _get().http_duration.record(
        seconds,
        {
            "http.request.method": method,
            "http.route": route,
            "http.response.status_code": status_code or 0,
        },
    )


def voice_connection_opened() -> None:
    _get().voice_connections_active.add(1)


def voice_connection_closed() -> None:
    _get().voice_connections_active.add(-1)


def record_voice_utterance(outcome: str) -> None:
    _get().voice_utterances.add(1, {"outcome": outcome})


def record_voice_dropped() -> None:
    _get().voice_dropped.add(1)


def record_stt(stt_seconds: float, audio_seconds: float, status: str) -> None:
    instruments = _get()
    attrs = {"status": status}
    instruments.stt_duration.record(stt_seconds, attrs)
    instruments.stt_audio_duration.record(audio_seconds, attrs)
    if audio_seconds > 0:
        instruments.stt_rtf.record(stt_seconds / audio_seconds, attrs)


# `JevFastPathResult.failure_reason` values for which
# app/agent/jev_fast_path.py returns *before* calling Jev. Their latency is
# not a Jev latency, so they are counted but not timed.
_JEV_NOT_CALLED = frozenset(
    {"unsupported_filter", "disabled", "jev_missing_api_key", "jev_sdk_missing"}
)


def record_jev_consultation(result: Any) -> None:
    """`result` is a `JevFastPathResult`. outcome: matched / failed (no
    usable answer: error, timeout, missing SDK or key) / declined (answered,
    but not a confident known command)."""
    if result.matched:
        outcome = "matched"
    elif result.is_failure:
        outcome = "failed"
    else:
        outcome = "declined"
    instruments = _get()
    instruments.jev_consultations.add(1, {"outcome": outcome})
    if result.failure_reason not in _JEV_NOT_CALLED:
        instruments.jev_duration.record(result.latency_ms / 1000, {"outcome": outcome})
    if result.input_tokens:
        instruments.jev_tokens.add(result.input_tokens, {"token_type": "input"})
    if result.output_tokens:
        instruments.jev_tokens.add(result.output_tokens, {"token_type": "output"})


def record_agent_turn(rounds: int, terminated_reason: str | None) -> None:
    termination = terminated_reason or "completed"
    instruments = _get()
    instruments.agent_rounds.record(rounds, {"termination": termination})
    instruments.agent_terminations.add(1, {"termination": termination})


def record_llm_call(seconds: float, status: str, model: str, usage: dict | None = None) -> None:
    instruments = _get()
    instruments.llm_duration.record(seconds, {"status": status, "model": model})
    for token_type in ("input", "output"):
        count = (usage or {}).get(f"{token_type}_tokens")
        if count:
            instruments.llm_tokens.add(count, {"token_type": token_type, "model": model})


def record_tool_call(
    tool_name: str,
    status: str,
    error_category: str | None = None,
    seconds: float | None = None,
) -> None:
    """`tool_name` must be a registered tool name or `UNKNOWN_TOOL`, never
    the model's raw string for an unknown tool. `seconds` only for calls
    that were actually executed."""
    instruments = _get()
    instruments.tool_calls.add(
        1,
        {"tool_name": tool_name, "status": status.lower(), "error_category": error_category or NONE},
    )
    if seconds is not None:
        instruments.tool_duration.record(seconds, {"tool_name": tool_name, "status": status.lower()})


def record_checkpointer_operation(operation: str, seconds: float, status: str) -> None:
    _get().checkpointer_duration.record(seconds, {"operation": operation, "status": status})

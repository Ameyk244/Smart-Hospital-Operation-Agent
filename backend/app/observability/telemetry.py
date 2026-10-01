"""OpenTelemetry SDK setup: turns export on, or leaves it off (observability
Phases 3 and 4).

Why it exists: the app records telemetry through the OTel *API* --
metrics in `metrics.py`, spans in `spans.py`, log records in
`otel_logs.py`. The API alone does nothing: its default providers are
no-ops, so with telemetry off every `add`/`record`/span is a cheap call into
an empty implementation and nothing leaves the process. This module is the
only place that installs the *SDK*:

- a `MeterProvider` with a periodic OTLP/HTTP metric reader (Phase 3);
- a `TracerProvider` with a `BatchSpanProcessor` feeding the OTLP/HTTP span
  exporter, wrapped in `spans.RedactingSpanExporter` so URL query strings
  and exception messages never leave the process (Phase 4);
- a `LoggerProvider` with a batch OTLP/HTTP log exporter, fed by the
  structlog processor in `otel_logs.py` with the already-redacted event
  dict (Phase 4);
- auto-instrumentation for FastAPI, SQLAlchemy (on the async engine's
  `sync_engine`), asyncpg and httpx (Phase 4).

All three signals share one `Resource` (`service.name=hospital-ops-backend`,
version, environment) and push to the collector (`grafana/otel-lgtm`,
Phase 5) at `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/{metrics,traces,logs}`.
That API/SDK split is the point: instrumented code never knows or cares
whether export is on.

Off by default (`OTEL_ENABLED=false`), so a fresh clone, the test suite and
CI never try to reach a collector. Even when on, a collector that is down
only costs failed background exports: readers and batch processors run in
their own threads and log export errors, they never raise into a request.
Each signal is set up in its own `try`, so one failing can't stop the app
or the other two. Shutdown flushes each with a short timeout.

Auto-instrumentation choices:
- FastAPI gets a no-op `MeterProvider`, so it emits traces only. The
  request middleware already records `http.server.request.duration` with
  controlled attributes; a second copy would double-count.
- `/api/health` is excluded, as are the per-message ASGI `receive`/`send`
  spans, which would add hundreds of spans per voice connection.
- SQLAlchemy's commenter is off (it would rewrite the SQL sent to
  Postgres), and asyncpg does not capture parameters (the default), so SQL
  spans carry parameterized statements, never values.
- SQLAlchemy and asyncpg both produce a span per query (the asyncpg one
  nested under the SQLAlchemy one). That is redundant but cheap, and it
  separates ORM time from driver time.

What calls it: `app/main.py`, at import time, because instrumentation has
to happen before the app builds its middleware stack, which Starlette does
on the first ASGI call (the lifespan one included). `shutdown_telemetry`
runs from the lifespan's exit.
"""

from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)

from app.config import Settings
from app.observability import metrics, otel_logs, spans
from app.observability.logging_config import get_logger

_logger = get_logger("observability.telemetry")

SERVICE_NAME = "hospital-ops-backend"
SERVICE_VERSION = "0.1.0"

# Short: a down collector should not hold up shutdown for long.
_EXPORT_TIMEOUT_MS = 5_000
_EXPORT_TIMEOUT_S = _EXPORT_TIMEOUT_MS / 1000

_EXCLUDED_URLS = "api/health"


@dataclass
class TelemetryHandle:
    meter_provider: MeterProvider | None = None
    tracer_provider: TracerProvider | None = None
    logger_provider: Any = None


def build_resource(settings: Settings) -> Resource:
    return Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "deployment.environment.name": settings.app_env,
        }
    )


def build_tracer_provider(
    resource: Resource, exporter: SpanExporter, *, batch: bool = True
) -> TracerProvider:
    """The one place the span pipeline is assembled, so tests (in-memory
    exporter, `batch=False`) go through exactly the production redaction."""
    provider = TracerProvider(resource=resource)
    redacting = spans.RedactingSpanExporter(exporter)
    processor = BatchSpanProcessor(redacting) if batch else SimpleSpanProcessor(redacting)
    provider.add_span_processor(processor)
    return provider


def instrument_libraries(
    tracer_provider: TracerProvider, *, app: Any = None, engine: Any = None
) -> None:
    """FastAPI (when `app` is given), SQLAlchemy (when `engine` is given),
    asyncpg and httpx. Must run before `app` handles its first ASGI call."""
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    no_metrics = NoOpMeterProvider()
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=tracer_provider,
            meter_provider=no_metrics,
            excluded_urls=_EXCLUDED_URLS,
            exclude_spans=["receive", "send"],
        )
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(
            engine=engine.sync_engine,
            tracer_provider=tracer_provider,
            meter_provider=no_metrics,
            enable_commenter=False,
        )
    AsyncPGInstrumentor().instrument(tracer_provider=tracer_provider)
    HTTPXClientInstrumentor().instrument(
        tracer_provider=tracer_provider, meter_provider=no_metrics
    )


def uninstrument_libraries(*, app: Any = None) -> None:
    """Reverse of `instrument_libraries` (used by tests)."""
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.uninstrument_app(app)
    for instrumentor in (SQLAlchemyInstrumentor(), AsyncPGInstrumentor(), HTTPXClientInstrumentor()):
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()


def _setup_metrics(settings: Settings, resource: Resource, endpoint: str) -> MeterProvider:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", timeout=_EXPORT_TIMEOUT_S),
        export_interval_millis=settings.otel_metric_export_interval_ms,
        export_timeout_millis=_EXPORT_TIMEOUT_MS,
    )
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    otel_metrics.set_meter_provider(provider)
    metrics.use_meter_provider(provider)
    return provider


def _setup_traces(resource: Resource, endpoint: str, *, app: Any, engine: Any) -> TracerProvider:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = build_tracer_provider(
        resource, OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", timeout=_EXPORT_TIMEOUT_S)
    )
    trace.set_tracer_provider(provider)
    spans.use_tracer_provider(provider)
    instrument_libraries(provider, app=app, engine=engine)
    return provider


def _setup_logs(resource: Resource, endpoint: str) -> Any:
    # Underscore modules: the OTel Python logs API is not yet declared
    # stable. Confined to here and otel_logs.py; see that module.
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{endpoint}/v1/logs", timeout=_EXPORT_TIMEOUT_S)
        )
    )
    otel_logs.use_logger(provider.get_logger("hospital_ops"))
    return provider


def setup_telemetry(settings: Settings, *, engine: Any = None, app: Any = None) -> TelemetryHandle:
    """Install the SDK when `settings.otel_enabled`, else do nothing (the
    API stays a no-op). Registers the SQLAlchemy engine for the pool gauge
    either way; the gauge is only ever read by an SDK reader."""
    if engine is not None:
        metrics.observe_sqlalchemy_pool(engine)
    handle = TelemetryHandle()
    if not settings.otel_enabled:
        return handle

    resource = build_resource(settings)
    endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
    try:
        handle.meter_provider = _setup_metrics(settings, resource, endpoint)
    except Exception as exc:  # noqa: BLE001 - telemetry must never stop the app
        _logger.warning("telemetry_setup_failed", signal="metrics", error=type(exc).__name__)
    try:
        handle.tracer_provider = _setup_traces(resource, endpoint, app=app, engine=engine)
    except Exception as exc:  # noqa: BLE001 - see above
        _logger.warning("telemetry_setup_failed", signal="traces", error=type(exc).__name__)
    try:
        handle.logger_provider = _setup_logs(resource, endpoint)
    except Exception as exc:  # noqa: BLE001 - see above
        _logger.warning("telemetry_setup_failed", signal="logs", error=type(exc).__name__)

    _logger.info("telemetry_enabled", endpoint=endpoint)
    return handle


def shutdown_telemetry(handle: TelemetryHandle) -> None:
    """Flush and stop every exporter. Never raises."""
    otel_logs.use_logger(None)
    for name, provider in (
        ("traces", handle.tracer_provider),
        ("logs", handle.logger_provider),
        ("metrics", handle.meter_provider),
    ):
        if provider is None:
            continue
        try:
            if name == "metrics":
                provider.shutdown(timeout_millis=_EXPORT_TIMEOUT_MS)
            else:
                provider.shutdown()
        except Exception as exc:  # noqa: BLE001 - see docstring
            _logger.warning("telemetry_shutdown_failed", signal=name, error=type(exc).__name__)

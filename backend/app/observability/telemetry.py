"""OpenTelemetry SDK setup: turns metric export on, or leaves it off
(observability Phase 3).

Why it exists: the app records metrics through the OTel *API*
(`app/observability/metrics.py`). The API alone does nothing: its default
provider is a no-op, so with telemetry off every `add`/`record` is a cheap
call into an empty implementation and nothing leaves the process. This
module is the only place that installs the *SDK* -- a real
`MeterProvider` with a `Resource` (`service.name=hospital-ops-backend`,
version, environment) and a periodic reader that pushes over OTLP/HTTP to
the collector (`grafana/otel-lgtm`, Phase 5). That API/SDK split is the
point: instrumented code never knows or cares whether export is on.

Off by default (`OTEL_ENABLED=false`), so a fresh clone, the test suite and
CI never try to reach a collector. The operator sets `OTEL_ENABLED=true` in
`.env` when the stack is running. Even then, a collector that is down only
costs failed background exports: the periodic reader runs in its own
thread and logs export errors, it never raises into a request. Setup and
shutdown are wrapped so a broken telemetry config can't stop the app from
starting or exiting.

What calls it: `app/main.py`'s lifespan (`setup_telemetry` at startup,
`shutdown_telemetry` at exit).
"""

from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from app.config import Settings
from app.observability import metrics
from app.observability.logging_config import get_logger

_logger = get_logger("observability.telemetry")

SERVICE_NAME = "hospital-ops-backend"
SERVICE_VERSION = "0.1.0"

# Short: a down collector should not hold up shutdown for long.
_EXPORT_TIMEOUT_MS = 5_000


@dataclass
class TelemetryHandle:
    meter_provider: MeterProvider | None = None


def build_resource(settings: Settings) -> Resource:
    return Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "deployment.environment.name": settings.app_env,
        }
    )


def setup_telemetry(settings: Settings, *, engine: Any = None) -> TelemetryHandle:
    """Install the SDK when `settings.otel_enabled`, else do nothing (the
    API stays a no-op). Registers the SQLAlchemy engine for the pool gauge
    either way; the gauge is only ever read by an SDK reader."""
    if engine is not None:
        metrics.observe_sqlalchemy_pool(engine)
    if not settings.otel_enabled:
        return TelemetryHandle()

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
            export_interval_millis=settings.otel_metric_export_interval_ms,
            export_timeout_millis=_EXPORT_TIMEOUT_MS,
        )
        provider = MeterProvider(resource=build_resource(settings), metric_readers=[reader])
        otel_metrics.set_meter_provider(provider)
        metrics.use_meter_provider(provider)
    except Exception as exc:  # noqa: BLE001 - telemetry must never stop the app
        _logger.warning("telemetry_setup_failed", error=type(exc).__name__)
        return TelemetryHandle()

    _logger.info("telemetry_enabled", endpoint=endpoint)
    return TelemetryHandle(meter_provider=provider)


def shutdown_telemetry(handle: TelemetryHandle) -> None:
    """Flush and stop the exporter. Never raises."""
    if handle.meter_provider is None:
        return
    try:
        handle.meter_provider.shutdown(timeout_millis=_EXPORT_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001 - see docstring
        _logger.warning("telemetry_shutdown_failed", error=type(exc).__name__)

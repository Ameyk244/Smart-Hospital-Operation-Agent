"""The telemetry on/off switch (`app/observability/telemetry.py`). No
collector is running for any of these; that is the point."""

import time

import structlog
from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider

from app.config import Settings
from app.observability import metrics, otel_logs, spans, telemetry


def test_off_by_default():
    assert Settings.model_fields["otel_enabled"].default is False


def test_disabled_installs_nothing_and_recording_is_a_noop():
    handle = telemetry.setup_telemetry(Settings(otel_enabled=False))
    assert handle.meter_provider is None
    assert not isinstance(otel_metrics.get_meter_provider(), MeterProvider)
    # Every helper must be callable with no SDK installed.
    metrics.record_chat_request("parser", "text", 0.01)
    metrics.record_tool_call("search_appointments", "SUCCESS", None, 0.01)
    metrics.record_http_request("GET", "/api/health", 200, 0.001)
    telemetry.shutdown_telemetry(handle)


def test_enabled_with_no_collector_never_raises(monkeypatch):
    """Port 9 (discard) has nothing listening: every export fails. Setup,
    recording a metric, a span and a log line, and shutdown must all still
    return normally. The global providers are not touched (OTel allows
    setting each only once per process)."""
    monkeypatch.setattr(telemetry.otel_metrics, "set_meter_provider", lambda _p: None)
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", lambda _p: None)
    settings = Settings(
        otel_enabled=True,
        otel_exporter_otlp_endpoint="http://127.0.0.1:9",
        otel_metric_export_interval_ms=60_000,
    )
    try:
        handle = telemetry.setup_telemetry(settings)
        assert isinstance(handle.meter_provider, MeterProvider)
        assert handle.tracer_provider is not None
        assert handle.logger_provider is not None
        resource = telemetry.build_resource(settings).attributes
        assert resource["service.name"] == "hospital-ops-backend"

        metrics.record_chat_request("parser", "text", 0.01)
        with spans.span("test.span"):
            structlog.get_logger("test.telemetry").info("exported_line")
        started = time.perf_counter()
        telemetry.shutdown_telemetry(handle)
        assert time.perf_counter() - started < 15
    finally:
        metrics.use_meter_provider(NoOpMeterProvider())
        spans.use_tracer_provider(None)
        otel_logs.use_logger(None)
        telemetry.uninstrument_libraries()

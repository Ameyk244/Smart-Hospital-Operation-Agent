"""Two structlog processors that tie logs to traces (observability Phase 4).

Why it exists:
- `add_trace_context` puts the current span's `trace_id`/`span_id` (hex,
  the same form Tempo shows) on every log line written inside a span. In
  Grafana that is the jump from a log line in Loki to its trace in Tempo
  and back. With telemetry off there is never a recording span, so nothing
  is added and stdout looks exactly as before.
- `export_to_otlp` ships each log line to the collector (Loki) as an OTLP
  log record, but only once `telemetry.py` has bound a logger provider
  (`OTEL_ENABLED=true`). Otherwise it returns the event untouched.

Why this design (least invasive): structlog already builds the event dict
and already redacts it (`redaction.make_log_redactor`). `export_to_otlp`
sits in the chain *after* the redactor and *before* `JSONRenderer`, so the
exported record is the same already-redacted dict that is printed, and
stdout output is unchanged. No stdlib `logging` handler bridge and no
second formatting path, which could drift from the redaction. It never
raises: an export problem drops the record, never the log line.

Caveat, said plainly: the OTel Python logs API still lives in underscore
modules (`opentelemetry._logs`, `opentelemetry.sdk._logs`), which upstream
treats as not yet stable. That is why the pin in requirements.txt is
narrow, and why everything touching it is confined to this file and
`telemetry.py`. stdlib-only loggers (uvicorn's access log) are not
exported; the structured `request_completed` line covers the same
requests.

What calls it: `logging_config.configure_logging` (processor chain),
`telemetry.setup_telemetry` (`use_logger`).
"""

import json
from typing import Any

from opentelemetry import trace
from opentelemetry._logs import Logger, SeverityNumber

_otel_logger: Logger | None = None

_SEVERITY = {
    "debug": SeverityNumber.DEBUG,
    "info": SeverityNumber.INFO,
    "warning": SeverityNumber.WARN,
    "warn": SeverityNumber.WARN,
    "error": SeverityNumber.ERROR,
    "exception": SeverityNumber.ERROR,
    "critical": SeverityNumber.FATAL,
}

# Not repeated as attributes: the body and the record's own fields carry them.
_NOT_ATTRIBUTES = frozenset({"event", "level", "timestamp"})


def use_logger(logger: Logger | None) -> None:
    global _otel_logger
    _otel_logger = logger


def add_trace_context(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def _attribute_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)):
        return value
    return json.dumps(value, default=str)


def export_to_otlp(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    logger = _otel_logger
    if logger is None:
        return event_dict
    try:
        level = str(event_dict.get("level", method)).lower()
        logger.emit(
            body=str(event_dict.get("event", "")),
            severity_text=level.upper(),
            severity_number=_SEVERITY.get(level, SeverityNumber.INFO),
            attributes={
                key: _attribute_value(value)
                for key, value in event_dict.items()
                if key not in _NOT_ATTRIBUTES and value is not None
            },
        )
    except Exception:  # noqa: BLE001 - see module docstring
        pass
    return event_dict

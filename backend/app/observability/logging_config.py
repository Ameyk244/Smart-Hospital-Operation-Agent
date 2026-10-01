"""Structured logging setup (concept 49).

Why it exists: one call, made once at process startup, so every log line in
the process is JSON with consistent fields — greppable/parseable, unlike
default `print`-style logging. Separate from `AgentEvent` (the DB-backed
trace `app/observability/tracing.py` writes): this is for operational logs
(every request, every error); the DB trace is for the UI's per-session trace
panel. They overlap in content but serve different readers.

Every line passes through `redaction.make_log_redactor` last, just before
rendering, so that no log line carries a credential or a known free-text
field. The uvicorn access log gets a filter that drops query strings (see
`app/observability/redaction.py`).

Since observability Phase 4, a line written inside an OpenTelemetry span
also carries `trace_id`/`span_id`, and with OTEL_ENABLED the redacted line
is also exported over OTLP (to Loki). See `otel_logs.py`.
"""

import logging
import sys

import structlog

from app.config import get_settings
from app.observability.otel_logs import add_trace_context, export_to_otlp
from app.observability.redaction import (
    AccessLogQueryStringFilter,
    make_log_redactor,
    secret_values_from_settings,
)


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=settings.log_level.upper()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_trace_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            make_log_redactor(secret_values_from_settings(settings)),
            # After the redactor on purpose: only the redacted dict is
            # exported. A no-op unless OTEL_ENABLED. See otel_logs.py.
            export_to_otlp,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogQueryStringFilter) for f in access_logger.filters):
        access_logger.addFilter(AccessLogQueryStringFilter())


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)

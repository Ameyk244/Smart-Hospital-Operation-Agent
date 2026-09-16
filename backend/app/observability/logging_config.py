"""Structured logging setup (concept 49).

Why it exists: one call, made once at process startup, so every log line in
the process is JSON with consistent fields — greppable/parseable, unlike
default `print`-style logging. Separate from `AgentEvent` (the DB-backed
trace `app/observability/tracing.py` writes): this is for operational logs
(every request, every error); the DB trace is for the UI's per-session trace
panel. They overlap in content but serve different readers.
"""

import logging
import sys

import structlog

from app.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=settings.log_level.upper()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)

"""Manual OpenTelemetry spans along the request path, and the exporter-side
redaction every span passes through (observability Phase 4).

Why it exists: auto-instrumentation (FastAPI, SQLAlchemy, asyncpg, httpx --
see `telemetry.py`) shows the HTTP request and each SQL query, but not
*why* a request took the path it did. The manual spans opened through
`span()` fill that in, so one `/api/chat` request is one connected trace:

    POST /api/chat                     (FastAPI auto, root; request_id attribute)
    └ chat.handle                      (route, channel, session.id)
      ├ parser.parse / command.execute
      ├ domain_gate.check / eligibility.check
      ├ jev.consult
      └ agent.run                      (rounds, termination)
        └ agent.round                  (one per model round)
          ├ llm.invoke                 (model, tokens, status)
          └ tool.execute               (tool name, status, error category)
            └ SQL spans                (SQLAlchemy / asyncpg auto)

Not the same thing as `tracing.py`: that writes the `agent_events` rows the
UI's trace panel reads. This is operator-facing, exported to Tempo; both
exist side by side and neither replaces the other.

PHI rules, enforced in two places:
- At the call sites: attributes are only enums, counts, durations, the
  session id and fixed config values. Never user text, a transcript, a
  query, tool arguments or a model-invented tool name.
- At export (`RedactingSpanExporter`), for spans we don't write ourselves:
  query strings are stripped from URL attributes (the ASGI instrumentation
  records `?query=<patient name>` in `http.target`/`http.url`), and
  exception events and error descriptions keep only the exception *type*.
  Exception messages can carry user text; FastAPI's instrumentation
  records them in full. `span()` itself never records messages.

API vs SDK: like `metrics.py`, spans are created from the API's global
tracer provider (a no-op until `telemetry.py` installs the SDK) unless a
provider is bound with `use_tracer_provider`, which tests do with an
in-memory exporter.

What calls it: `chat.py`, `graph.py`, `voice.py`, `stt.py`,
`checkpointer.py`, `request_context.py`; `telemetry.py` for the exporter.
"""

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Span, Status, StatusCode, TracerProvider

TRACER_NAME = "hospital_ops"

_tracer_provider: TracerProvider | None = None


def use_tracer_provider(provider: TracerProvider | None) -> None:
    """Bind spans to `provider` (None = back to the global one)."""
    global _tracer_provider
    _tracer_provider = provider


def get_tracer() -> trace.Tracer:
    if _tracer_provider is not None:
        return _tracer_provider.get_tracer(TRACER_NAME)
    return trace.get_tracer(TRACER_NAME)


def _clean(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    # OTel rejects None values (with a warning); drop them instead.
    return {k: v for k, v in (attributes or {}).items() if v is not None}


def set_attributes(span: Span, **attributes: Any) -> None:
    span.set_attributes(_clean(attributes))


def mark_error(span: Span, error: BaseException | str) -> None:
    """ERROR status plus an `exception` event carrying only the type. The
    message is deliberately not recorded: it can contain user text."""
    error_type = error if isinstance(error, str) else type(error).__name__
    span.set_status(Status(StatusCode.ERROR, error_type))
    span.add_event("exception", {"exception.type": error_type})


@contextmanager
def span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    context: Context | None = None,
    links: Sequence[Link] | None = None,
) -> Iterator[Span]:
    """A span that is current for the block. An exception escaping the
    block marks it ERROR (type only) and propagates unchanged."""
    with get_tracer().start_as_current_span(
        name,
        context=context,
        links=links,
        attributes=_clean(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except Exception as exc:
            mark_error(current, exc)
            raise


# --- Export-side redaction ----------------------------------------------------

_DROPPED_ATTRIBUTES = frozenset({"url.query"})
_URL_ATTRIBUTE_PREFIXES = ("http.", "url.")
_DROPPED_EVENT_ATTRIBUTES = frozenset({"exception.message", "exception.stacktrace"})
_TYPE_PREFIX = re.compile(r"[A-Za-z_][\w.]*")


def _redact_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    redacted = {}
    for key, value in (attributes or {}).items():
        if key in _DROPPED_ATTRIBUTES:
            continue
        if key.startswith(_URL_ATTRIBUTE_PREFIXES) and isinstance(value, str) and "?" in value:
            value = value.split("?", 1)[0]
        redacted[key] = value
    return redacted


def redact_span(span_data: ReadableSpan) -> ReadableSpan:
    status = span_data.status
    if status.description:
        match = _TYPE_PREFIX.match(status.description)
        status = Status(status.status_code, match.group(0) if match else None)
    events = [
        Event(
            event.name,
            {
                k: v
                for k, v in (event.attributes or {}).items()
                if k not in _DROPPED_EVENT_ATTRIBUTES
            },
            event.timestamp,
        )
        for event in span_data.events
    ]
    return ReadableSpan(
        name=span_data.name,
        context=span_data.context,
        parent=span_data.parent,
        resource=span_data.resource,
        attributes=_redact_attributes(span_data.attributes),
        events=events,
        links=span_data.links,
        kind=span_data.kind,
        status=status,
        start_time=span_data.start_time,
        end_time=span_data.end_time,
        instrumentation_scope=span_data.instrumentation_scope,
    )


class RedactingSpanExporter(SpanExporter):
    """Wraps the real exporter (OTLP in production, in-memory in tests) so
    every span, ours or auto-instrumented, is redacted before it leaves the
    process. Fail-closed on the two shapes known to carry free text: URL
    query strings and exception messages."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([redact_span(s) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)

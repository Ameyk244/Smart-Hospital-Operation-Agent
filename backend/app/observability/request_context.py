"""Request correlation: one `request_id` per HTTP request / WebSocket
connection, carried on every log line it causes (observability Phase 2).

Why it exists: before this, the JSON logs had no way to tell which lines
belonged to the same request -- two concurrent chats interleave on stdout
and an `agent.trace` line can't be tied to the HTTP call that caused it.
This middleware binds a `request_id` into structlog's contextvars (already
merged into every line by `merge_contextvars` in `logging_config.py`), so
every line logged anywhere downstream -- route, agent graph, tool, trace
event -- carries it without any call site passing it explicitly. The same
id is echoed back in the `X-Request-ID` response header so a client report
("this request was slow") can be matched to its log lines.

It also writes one `request_completed` line per HTTP request (method, route
template, status code, duration_ms), so the logs alone give RED (rate,
errors, duration) per endpoint. Since Phase 3 the same numbers also go to
the `http.server.request.duration` histogram (`app/observability/metrics.py`).

Deliberate choices:
- Pure ASGI, not `BaseHTTPMiddleware`. `BaseHTTPMiddleware` runs the
  endpoint in a separate task, which complicates contextvar propagation and
  buffers streaming responses; a plain ASGI wrapper runs in the request's
  own task, so a value bound here is visible all the way down and is reset
  here afterwards.
- An incoming `X-Request-ID` is accepted only if it is a short, safe token
  (`_SAFE_REQUEST_ID`). Anything else -- too long, spaces, quotes, newlines
  -- is replaced with a fresh id, so a client can't inject arbitrary text
  (or a patient name) into every log line of its request.
- The route is the *template* (`/api/sessions/{session_id}/trace`), never
  the raw path: the raw path carries ids and the query string carries
  search text (`?query=<patient name>`). Neither the query string nor the
  body is ever logged. Unmatched paths log as `unmatched`.
- Bindings are reset with the tokens `bind_contextvars` returns (not
  `clear_contextvars`), so the previous state is restored exactly and
  nothing bleeds into the next request even when the caller runs requests
  in the same task (as `httpx.ASGITransport` does in tests).
- WebSocket connections get a `request_id` for the connection's whole
  lifetime (every voice utterance logged on it shares it), echoed as an
  `X-Request-ID` header on the accept, and one `websocket_closed` line with
  the route template and duration.

What calls it: `app/main.py` adds it as the outermost user middleware.
"""

import re
import time
import uuid
from typing import Any

import structlog
from opentelemetry import trace

from app.observability import metrics
from app.observability.logging_config import get_logger

_logger = get_logger("api.request")

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("latin-1")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_UNMATCHED_ROUTE = "unmatched"


def resolve_request_id(incoming: str | None) -> str:
    """The client's id if it is a safe short token, otherwise a new one."""
    if incoming and _SAFE_REQUEST_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


def _incoming_request_id(scope: dict[str, Any]) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == _REQUEST_ID_HEADER_BYTES:
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes any byte
                return None
    return None


def _route_template(scope: dict[str, Any]) -> str:
    """FastAPI's router stores the matched route object in `scope["route"]`
    (fastapi/routing.py, both `APIRoute` and `APIWebSocketRoute`); its
    `.path` is the template with `{param}` placeholders. Read after the app
    has run, since routing fills it in on the same scope dict."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else _UNMATCHED_ROUTE


class RequestContextMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(_incoming_request_id(scope))
        header = (_REQUEST_ID_HEADER_BYTES, request_id.encode("latin-1"))
        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        # Phase 4: also on the request's root span (FastAPI's server span,
        # current here when tracing is on; a no-op otherwise). Spans may
        # carry per-request ids; metrics never do.
        trace.get_current_span().set_attribute("request_id", request_id)
        started = time.perf_counter()
        status_code: int | None = None

        async def send_with_request_id(message: dict[str, Any]) -> None:
            nonlocal status_code
            message_type = message.get("type")
            if message_type in ("http.response.start", "websocket.accept"):
                if message_type == "http.response.start":
                    status_code = message.get("status")
                message = dict(message)
                message["headers"] = [*(message.get("headers") or ()), header]
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            # The exception propagates to Starlette's ServerErrorMiddleware,
            # which turns it into a 500 outside this middleware -- record it
            # as one here so errors are not missing from the log-based RED.
            if status_code is None:
                status_code = 500
            raise
        finally:
            elapsed = time.perf_counter() - started
            duration_ms = round(elapsed * 1000, 1)
            route = _route_template(scope)
            if scope_type == "http":
                # Same numbers as the log line, as a histogram (Phase 3).
                metrics.record_http_request(scope.get("method"), route, status_code, elapsed)
                _logger.info(
                    "request_completed",
                    method=scope.get("method"),
                    route=route,
                    status_code=status_code,
                    duration_ms=duration_ms,
                )
            else:
                _logger.info("websocket_closed", route=route, duration_ms=duration_ms)
            structlog.contextvars.reset_contextvars(**tokens)

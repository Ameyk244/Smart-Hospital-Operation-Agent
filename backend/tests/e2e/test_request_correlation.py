"""Request correlation through the real FastAPI app (observability Phase 2).

Proves, against the real middleware stack and real routes: every log line
a request causes carries one `request_id`, which is echoed in the
`X-Request-ID` header; an unsafe incoming id is replaced; nothing bound for
one request is still bound for the next; a chat turn's lines carry its
`session_id`; and the per-request `request_completed` line logs the route
template, never the raw path or query string.

Uses the shared `client` fixture (tests/conftest.py), which runs requests
through `httpx.ASGITransport` in the test's own task -- so a binding the
middleware failed to reset would still be visible to the test afterwards,
which is exactly what the leak test checks. Logs come from the
`captured_logs` fixture (also tests/conftest.py).
"""

import re

import pytest
import structlog

import app.api.routes.chat as chat_module
from app.observability.logging_config import get_logger

# Imported at module level on purpose, like tests/e2e/test_chat_api.py:
# app.main sets the Windows event-loop policy at import time, and doing that
# for the first time inside a test (via the `client` fixture) swaps the
# policy under pytest-asyncio's already-running loop.
from app.main import app  # noqa: F401

pytestmark = pytest.mark.integration

_GENERATED_ID = re.compile(r"^[0-9a-f]{32}$")


def _lines_for(captured_logs, request_id: str) -> list[dict]:
    return [line for line in captured_logs.lines() if line.get("request_id") == request_id]


async def test_every_log_line_of_a_request_shares_the_echoed_request_id(client, captured_logs):
    response = await client.post("/api/chat", json={"text": "list departments"})
    assert response.status_code == 200

    request_id = response.headers["X-Request-ID"]
    assert _GENERATED_ID.fullmatch(request_id)

    lines = captured_logs.lines()
    assert lines, "the request produced no log lines at all"
    assert {line.get("request_id") for line in lines} == {request_id}

    completed = captured_logs.events("request_completed")
    assert len(completed) == 1
    assert completed[0]["method"] == "POST"
    assert completed[0]["route"] == "/api/chat"
    assert completed[0]["status_code"] == 200
    assert isinstance(completed[0]["duration_ms"], (int, float))


async def test_a_safe_incoming_request_id_is_kept(client, captured_logs):
    response = await client.get("/api/health", headers={"X-Request-ID": "client-abc.123_X"})
    assert response.headers["X-Request-ID"] == "client-abc.123_X"
    assert captured_logs.events("request_completed")[0]["request_id"] == "client-abc.123_X"


@pytest.mark.parametrize(
    "unsafe",
    [
        "show patient David Davis",  # spaces: free text must not ride into every log line
        "x" * 65,  # too long
        'abc"}{"injected":1',  # quotes/braces
        "",
    ],
)
async def test_an_unsafe_incoming_request_id_is_replaced(client, captured_logs, unsafe):
    response = await client.get("/api/health", headers={"X-Request-ID": unsafe})
    request_id = response.headers["X-Request-ID"]
    assert request_id != unsafe
    assert _GENERATED_ID.fullmatch(request_id)
    if unsafe:
        assert unsafe not in "\n".join(captured_logs.raw)
    assert captured_logs.events("request_completed")[0]["request_id"] == request_id


async def test_context_does_not_leak_between_sequential_requests(client, captured_logs):
    first = await client.post("/api/chat", json={"text": "list departments"})
    # The client runs the app in this very task: anything the middleware or
    # the handler left bound would still be visible here.
    assert "request_id" not in structlog.contextvars.get_contextvars()
    assert "session_id" not in structlog.contextvars.get_contextvars()

    second = await client.get("/api/health")
    assert "request_id" not in structlog.contextvars.get_contextvars()

    first_id = first.headers["X-Request-ID"]
    second_id = second.headers["X-Request-ID"]
    assert first_id != second_id

    second_lines = _lines_for(captured_logs, second_id)
    assert second_lines
    first_session = first.json()["session_id"]
    assert all(line.get("session_id") != first_session for line in second_lines)


async def test_chat_log_lines_carry_the_session_id(client, captured_logs, monkeypatch):
    """A line logged anywhere inside the routing -- without passing
    session_id itself -- must carry it. `parse` is wrapped to log one such
    line, since the deterministic path logs nothing of its own."""
    probe_logger = get_logger("test.correlation_probe")
    real_parse = chat_module.parse

    def logging_parse(text):
        probe_logger.info("probe_inside_routing")
        return real_parse(text)

    monkeypatch.setattr(chat_module, "parse", logging_parse)

    response = await client.post(
        "/api/chat", json={"text": "list departments", "session_id": "corr-session-1"}
    )
    assert response.status_code == 200

    probes = captured_logs.events("probe_inside_routing")
    assert len(probes) == 1
    assert probes[0]["session_id"] == "corr-session-1"
    assert probes[0]["request_id"] == response.headers["X-Request-ID"]


async def test_request_completed_logs_route_template_never_path_or_query(client, captured_logs):
    search = await client.get(
        "/api/operations/patients", params={"query": "David Davis"}
    )
    assert search.status_code == 200
    trace = await client.get("/api/sessions/some-session-xyz/trace")
    missing = await client.get("/api/does-not-exist")

    routes = [line["route"] for line in captured_logs.events("request_completed")]
    assert routes == [
        "/api/operations/patients",
        "/api/sessions/{session_id}/trace",
        "unmatched",
    ]
    statuses = [line["status_code"] for line in captured_logs.events("request_completed")]
    assert statuses == [200, trace.status_code, missing.status_code]

    printed = "\n".join(captured_logs.raw)
    assert "David" not in printed
    assert "some-session-xyz" not in printed
    assert "does-not-exist" not in printed

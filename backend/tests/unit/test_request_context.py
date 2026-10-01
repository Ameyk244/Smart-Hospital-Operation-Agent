"""Unit tests for `app/observability/request_context.py` against a tiny
throwaway FastAPI app -- no database, no real routes. Covers what the
real-app tests in tests/e2e/test_request_correlation.py can't reach
cheaply: an endpoint that raises, and a WebSocket connection.
"""

import pytest
import structlog
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from app.observability.logging_config import configure_logging, get_logger
from app.observability.request_context import RequestContextMiddleware, resolve_request_id

_probe = get_logger("test.request_context")


@pytest.fixture(autouse=True)
def _json_logging():
    # Unit tests don't import app.main, which is what normally configures
    # structlog; without this the lines would be structlog's default
    # console format rather than the JSON the app really prints.
    configure_logging()


def _toy_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/boom/{item_id}")
    async def boom(item_id: str):
        _probe.info("about_to_fail")
        raise RuntimeError("synthetic failure")

    @app.websocket("/ws/{session_id}")
    async def ws(websocket: WebSocket, session_id: str):
        await websocket.accept()
        _probe.info("ws_message", seen=await websocket.receive_text())
        await websocket.send_text("ok")
        _probe.info("ws_message", seen=await websocket.receive_text())
        await websocket.close()

    return app


@pytest.mark.parametrize("value", ["abc", "A.b-c_9", "x" * 64])
def test_safe_ids_are_kept(value):
    assert resolve_request_id(value) == value


@pytest.mark.parametrize("value", [None, "", "x" * 65, "has space", "new\nline", "é"])
def test_unsafe_ids_are_replaced(value):
    replaced = resolve_request_id(value)
    assert replaced != value
    assert len(replaced) == 32


def test_an_unhandled_error_is_logged_as_500_with_the_template(captured_logs):
    client = TestClient(_toy_app(), raise_server_exceptions=False)
    response = client.get("/boom/P-0001", headers={"X-Request-ID": "err-1"})
    assert response.status_code == 500

    completed = captured_logs.events("request_completed")
    assert len(completed) == 1
    assert completed[0]["status_code"] == 500
    assert completed[0]["route"] == "/boom/{item_id}"
    assert completed[0]["request_id"] == "err-1"
    assert captured_logs.events("about_to_fail")[0]["request_id"] == "err-1"
    assert "P-0001" not in "\n".join(captured_logs.raw)


def test_websocket_binds_one_request_id_for_the_whole_connection(captured_logs):
    client = TestClient(_toy_app())
    with client.websocket_connect("/ws/s1", headers={"X-Request-ID": "ws-1"}) as ws:
        accept_headers = dict(ws.extra_headers or [])
        ws.send_text("first")
        assert ws.receive_text() == "ok"
        ws.send_text("second")

    assert accept_headers[b"x-request-id"] == b"ws-1"
    messages = captured_logs.events("ws_message")
    assert [m["seen"] for m in messages] == ["first", "second"]
    assert {m["request_id"] for m in messages} == {"ws-1"}
    closed = captured_logs.events("websocket_closed")
    assert len(closed) == 1
    assert closed[0]["route"] == "/ws/{session_id}"
    assert closed[0]["request_id"] == "ws-1"


async def test_bindings_are_restored_not_cleared():
    """Resetting with tokens restores whatever was bound before the
    request, instead of wiping it, and drops what the request added."""
    sent = []

    async def app(scope, receive, send):
        assert structlog.contextvars.get_contextvars()["request_id"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = RequestContextMiddleware(app)
    with structlog.contextvars.bound_contextvars(outer="kept"):
        await middleware({"type": "http", "method": "GET", "headers": []}, receive, send)
        assert structlog.contextvars.get_contextvars() == {"outer": "kept"}

    headers = dict(sent[0]["headers"])
    assert len(headers[b"x-request-id"]) == 32

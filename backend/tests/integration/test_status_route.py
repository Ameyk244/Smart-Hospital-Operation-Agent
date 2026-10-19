"""`GET /api/status`: each light's state from configuration and, for the
harness, a real health probe against a local stand-in for the gateway."""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import app

TOKEN = "t" * 32
MODEL = "anthropic/claude-sonnet-5"
ANTHROPIC_SECRET = "anthropic-secret-value-1"
TYPESAFE_SECRET = "typesafe-secret-value-2"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
def configure(monkeypatch):
    def apply(**overrides) -> Settings:
        values = dict(
            agent_runtime="in_process",
            llm_provider="anthropic",
            anthropic_api_key=ANTHROPIC_SECRET,
            enable_jev_fast_path=True,
            typesafe_api_key=TYPESAFE_SECRET,
        )
        settings = Settings(**{**values, **overrides})
        monkeypatch.setattr("app.api.routes.status.get_settings", lambda: settings)
        return settings

    return apply


@pytest.fixture
async def gateway():
    """A one-route stand-in for the gateway's /healthz; yields its base URL."""
    servers = []

    async def start(body: str, status: int = 200) -> str:
        async def handle(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            payload = body.encode()
            head = f"HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {len(payload)}\r\nconnection: close\r\n\r\n"
            writer.write(head.encode() + payload)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        servers.append(server)
        return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()


async def lights(client) -> dict:
    response = await client.get("/api/status")
    assert response.status_code == 200
    return response.json()


async def test_in_process_with_keys_has_the_agent_and_jev_on_and_no_harness(client, configure):
    configure()

    assert await lights(client) == {
        "mode": 1,
        "agent": {"runtime": "in_process", "status": "on"},
        "harness": {"status": "off"},
        "jev": {"status": "on"},
    }


@pytest.mark.parametrize(
    "overrides",
    [{"enable_jev_fast_path": False}, {"typesafe_api_key": None}],
    ids=["disabled", "no-key"],
)
async def test_jev_is_off_when_disabled_or_without_a_key(client, configure, overrides):
    configure(**overrides)

    assert (await lights(client))["jev"] == {"status": "off"}


@pytest.mark.parametrize(
    ("provider", "overrides"),
    [
        ("anthropic", {"anthropic_api_key": None}),
        ("openrouter", {"openrouter_api_key": None}),
    ],
)
async def test_in_process_agent_is_off_without_the_providers_key(client, configure, provider, overrides):
    configure(llm_provider=provider, **overrides)

    assert (await lights(client))["agent"] == {"runtime": "in_process", "status": "off"}


async def test_harness_runtime_is_on_when_the_gateway_reports_the_expected_model(client, configure, gateway):
    url = await gateway(json.dumps({"status": "ok", "model": MODEL}))
    configure(agent_runtime="harness", harness_token=TOKEN, harness_url=url, harness_model=MODEL)

    body = await lights(client)

    assert body["agent"] == {"runtime": "harness", "status": "on"}
    assert body["harness"] == {"status": "on"}


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (json.dumps({"status": "ok", "model": "another/model"}), 200),
        (json.dumps({"status": "degraded", "model": MODEL}), 200),
        (json.dumps({"status": "ok", "model": MODEL}), 500),
        ("not json", 200),
        (json.dumps(["ok"]), 200),
    ],
    ids=["wrong-model", "not-ok", "http-error", "not-json", "not-an-object"],
)
async def test_harness_runtime_is_down_when_the_gateway_answers_wrongly(client, configure, gateway, body, status):
    url = await gateway(body, status)
    configure(agent_runtime="harness", harness_token=TOKEN, harness_url=url, harness_model=MODEL)

    result = await lights(client)

    assert result["harness"] == {"status": "down"}
    assert result["agent"] == {"runtime": "harness", "status": "down"}


async def test_harness_runtime_is_down_when_nothing_is_listening(client, configure):
    configure(agent_runtime="harness", harness_token=TOKEN, harness_url="http://127.0.0.1:9", harness_model=MODEL)

    assert (await lights(client))["harness"] == {"status": "down"}


async def test_the_response_exposes_no_url_model_or_key(client, configure, gateway):
    url = await gateway(json.dumps({"status": "ok", "model": MODEL}))
    configure(agent_runtime="harness", harness_token=TOKEN, harness_url=url, harness_model=MODEL)

    text = (await client.get("/api/status")).text

    for secret in (url, MODEL, TOKEN, ANTHROPIC_SECRET, TYPESAFE_SECRET):
        assert secret not in text


@pytest.mark.parametrize(
    ("runtime", "headers", "mode"),
    [
        ("in_process", {}, 1),
        ("in_process", {"x-front-door": "1"}, 1),
        ("harness", {}, 2),
        ("harness", {"x-front-door": "1"}, 3),
    ],
    ids=["mode-1", "relay-only-stays-1", "mode-2", "mode-3"],
)
async def test_the_run_mode_follows_the_runtime_and_the_front_door_mark(client, configure, runtime, headers, mode):
    configure(agent_runtime=runtime, harness_token=TOKEN, harness_url="http://127.0.0.1:9", harness_model=MODEL)

    response = await client.get("/api/status", headers=headers)

    assert response.json()["mode"] == mode

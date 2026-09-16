"""End-to-end tests for the unified /api/chat endpoint — the deterministic
half offline (no LLM), plus one gated live-model round trip proving the
*entire* request path (HTTP -> parser-or-agent -> tools -> Postgres ->
conversation history persisted) with a real model.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.routes.chat import get_checkpointer
from app.db.session import get_db, get_session_factory
from app.main import app

pytestmark = pytest.mark.integration

# `client` fixture is shared — see tests/conftest.py.


async def test_deterministic_request_via_chat_endpoint(client):
    response = await client.post("/api/chat", json={"text": "list departments"})
    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "deterministic"
    assert len(body["data"]) == 4
    assert body["session_id"]


async def test_empty_request_is_rejected_before_reaching_the_agent(client):
    response = await client.post("/api/chat", json={"text": "   "})
    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "rejected"


async def test_session_id_is_reused_across_turns(client):
    first = await client.post("/api/chat", json={"text": "list departments"})
    session_id = first.json()["session_id"]
    second = await client.post(
        "/api/chat", json={"text": "list scanners", "session_id": session_id}
    )
    assert second.json()["session_id"] == session_id


async def test_oversized_session_id_is_rejected_with_a_clean_422(client):
    """AgentSession.id is sized for a UUID (36 chars). A client-supplied
    session_id longer than that must fail request validation, not reach the
    database as an unhandled StringDataRightTruncationError."""
    response = await client.post(
        "/api/chat", json={"text": "list departments", "session_id": "x" * 100}
    )
    assert response.status_code == 422


@pytest.mark.live_llm
async def test_agent_path_via_http_with_live_model(agent_session_factory, checkpointer):
    """The full concept-58 chain through the real HTTP endpoint: request ->
    parser (unmatched) -> eligibility gate -> LangGraph agent (with real
    Postgres checkpointing) -> real Anthropic call -> real tool -> Postgres
    -> conversation history persisted -> HTTP response. Uses the test
    database via dependency overrides so it never touches dev data."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.run_live_llm_tests:
        pytest.skip("RUN_LIVE_LLM_TESTS is not set to 1")

    async def _override_get_db():
        async with agent_session_factory() as session:
            yield session

    def _override_get_session_factory():
        return agent_session_factory

    def _override_get_checkpointer():
        return checkpointer

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_session_factory] = _override_get_session_factory
    app.dependency_overrides[get_checkpointer] = _override_get_checkpointer
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/api/chat",
                json={"text": "How many delayed MRI appointments are there right now?"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "agent"
    assert body["message"]
    print(f"\nLive HTTP agent response: {body['message']}")

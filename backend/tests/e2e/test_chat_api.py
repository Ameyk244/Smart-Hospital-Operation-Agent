"""End-to-end tests for the unified /api/chat endpoint — the deterministic
half offline (no LLM), plus one gated live-model round trip proving the
*entire* request path (HTTP -> parser-or-agent -> tools -> Postgres ->
conversation history persisted) with a real model.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

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
    # The reply text itself must contain real data, not a generic
    # acknowledgment — asserting only that `message` is non-empty would let
    # a regression back to "OK — ran list_departments." pass silently, since
    # that string is also non-empty. Every department name must actually be
    # named in the reply the user reads.
    for department in body["data"]:
        assert department["name"] in body["message"]
    assert sorted(body["touched_entity_codes"]) == sorted(d["code"] for d in body["data"])


async def test_deterministic_scanner_search_reply_names_real_scanners(client):
    response = await client.post("/api/chat", json={"text": "list scanners mri available"})
    body = response.json()
    assert body["handled_by"] == "deterministic"
    assert body["data"]  # seed data guarantees >=1 AVAILABLE MRI scanner
    for scanner in body["data"]:
        assert scanner["code"] in body["message"]


async def test_deterministic_show_next_appointment_reply_names_the_appointment(client):
    response = await client.post("/api/chat", json={"text": "show next appointment"})
    body = response.json()
    assert body["handled_by"] == "deterministic"
    assert body["data"] is not None
    assert body["data"]["code"] in body["message"]
    assert body["data"]["patient"]["name"] in body["message"]


async def test_empty_request_is_rejected_before_reaching_the_agent(client):
    response = await client.post("/api/chat", json={"text": "   "})
    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "rejected"


async def test_off_topic_request_is_rejected_without_ever_invoking_the_llm(client):
    """The domain gate (extends concept 44) must reject an off-topic request
    entirely on its own — proving not just the response shape but that
    `get_default_chat_model` (and therefore no LLM client at all) is ever
    constructed or called is the actual point: this protects API spend, and
    a test that only checks `handled_by` wouldn't catch a regression where
    the gate rejects *after* the model was already built."""
    with patch("app.api.routes.chat.get_default_chat_model") as mock_get_model:
        response = await client.post(
            "/api/chat", json={"text": "what's the weather like today?"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "rejected"
    mock_get_model.assert_not_called()


async def test_bundled_off_topic_request_is_rejected_without_ever_invoking_the_llm(client):
    """Regression test for the real bypass found this session: the domain
    gate used to pass any message containing a domain word *anywhere*, even
    a disconnected word tacked onto an already-complete, unrelated
    sentence/question — e.g. this exact text, "What's 47 times 12? mri",
    would have reached the agent (and constructed a real LLM client) purely
    because "mri" appears somewhere in the message, despite the actual
    request being pure off-topic arithmetic. Proves both the response shape
    and, like the sibling test above, that `get_default_chat_model` is never
    constructed or called for it."""
    with patch("app.api.routes.chat.get_default_chat_model") as mock_get_model:
        response = await client.post(
            "/api/chat", json={"text": "What's 47 times 12? mri"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "rejected"
    mock_get_model.assert_not_called()


async def test_session_id_is_reused_across_turns(client):
    first = await client.post("/api/chat", json={"text": "list departments"})
    session_id = first.json()["session_id"]
    second = await client.post(
        "/api/chat", json={"text": "list scanners", "session_id": session_id}
    )
    assert second.json()["session_id"] == session_id


async def test_contextual_followup_reaches_agent_without_live_llm(client):
    first = await client.post("/api/chat", json={"text": "list departments"})
    session_id = first.json()["session_id"]
    final_state = {
        "messages": [AIMessage(content="I listed the hospital departments.")],
        "terminated_reason": None,
        "touched_entity_codes": [],
    }

    with (
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch(
            "app.api.routes.chat.run_agent",
            new=AsyncMock(return_value=final_state),
        ) as run,
    ):
        response = await client.post(
            "/api/chat",
            json={"text": "What happened?", "session_id": session_id},
        )

    assert response.status_code == 200
    assert response.json()["handled_by"] == "agent"
    run.assert_awaited_once()


async def test_a_bare_code_reschedule_reaches_the_agent(client):
    """Regression test: "reschedule APT-2001 to SCN-1" used to be refused by
    the domain gate, because entity codes tokenized to meaningless fragments
    and left no domain word behind — so the system's only write operation,
    phrased the most direct way, never reached the agent at all. It must now
    get there, where grounding decides whether those codes may be used."""
    final_state = {
        "messages": [AIMessage(content="I need to look those codes up first.")],
        "terminated_reason": None,
        "touched_entity_codes": [],
    }

    with (
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch(
            "app.api.routes.chat.run_agent",
            new=AsyncMock(return_value=final_state),
        ) as run,
    ):
        response = await client.post(
            "/api/chat", json={"text": "reschedule APT-2001 to SCN-1"}
        )

    assert response.status_code == 200
    assert response.json()["handled_by"] == "agent"
    run.assert_awaited_once()


@pytest.mark.adversarial
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

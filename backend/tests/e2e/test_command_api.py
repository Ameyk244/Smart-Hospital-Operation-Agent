"""End-to-end test (concept 58): natural request -> HTTP -> parser ->
CommandRunner -> PostgreSQL -> observable JSON result. No LLM in this path
yet (it's the deterministic half of the architecture) — the agent-involving
E2E case is added once the agent exists.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import get_db
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(seeded_session):
    async def _override_get_db():
        yield seeded_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_health(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_recognized_command_executes(client):
    response = await client.post("/api/commands", json={"text": "list departments"})
    assert response.status_code == 200
    body = response.json()
    assert body["matched"] is True
    assert body["success"] is True
    assert len(body["data"]) == 4


async def test_unrecognized_command_reports_unmatched(client):
    response = await client.post(
        "/api/commands", json={"text": "please reschedule my thing somehow"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["matched"] is False
    assert body["success"] is None


async def test_show_patient_returns_multiple_matches(client):
    response = await client.post("/api/commands", json={"text": "show patient a"})
    assert response.status_code == 200
    body = response.json()
    assert body["matched"] is True
    assert body["success"] is True
    assert len(body["data"]) > 1

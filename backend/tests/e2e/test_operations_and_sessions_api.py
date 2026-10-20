"""End-to-end tests for the read-only operations endpoints (the frontend's
main data view) and the session-scoped trace/messages endpoints (the
frontend's trace panel and chat history restore).
"""

import pytest

pytestmark = pytest.mark.integration

# `client` fixture is shared — see tests/conftest.py.


async def test_list_departments(client):
    response = await client.get("/api/operations/departments")
    assert response.status_code == 200
    assert len(response.json()) == 4


async def test_list_scanners_with_filters(client):
    response = await client.get("/api/operations/scanners", params={"type": "MRI"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) > 0
    assert all(s["type"] == "MRI" for s in body)


async def test_search_appointments(client):
    response = await client.get(
        "/api/operations/appointments", params={"status": "DELAYED", "appointment_type": "MRI"}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 2


async def test_search_patients_requires_query(client):
    response = await client.get("/api/operations/patients")
    assert response.status_code == 422  # query is a required param


async def test_search_patients(client):
    response = await client.get("/api/operations/patients", params={"query": "a"})
    assert response.status_code == 200
    assert len(response.json()) > 1


async def test_list_wards(client):
    response = await client.get("/api/operations/wards")
    assert response.status_code == 200
    assert response.json()[0].keys() == {"code", "name", "department", "bed_count", "occupied_count"}


async def test_list_beds_with_filters(client):
    response = await client.get(
        "/api/operations/beds", params={"ward_code": "WRD-1", "status": "AVAILABLE"}
    )
    assert response.status_code == 200
    body = response.json()
    assert [b["code"] for b in body] == ["BED-2", "BED-4"]
    assert body[0].keys() == {"code", "ward", "status", "patient", "occupied_since"}


async def test_queue_with_filters(client):
    response = await client.get(
        "/api/operations/queue", params={"urgency": "CRITICAL", "status": "WAITING"}
    )
    assert response.status_code == 200
    body = response.json()
    assert [e["code"] for e in body] == ["ER-3"]
    assert body[0].keys() == {
        "code", "patient", "urgency", "status", "arrived_at", "triaged_at",
        "bed", "discharged_at", "wait_minutes",
    }


async def test_journey_and_its_errors(client):
    response = await client.get("/api/operations/journey", params={"patient_code": "PT-1027"})
    assert response.status_code == 200
    assert response.json().keys() == {"patient", "stages", "current_stage", "appointments"}
    assert (await client.get("/api/operations/journey")).status_code == 422
    unknown = await client.get("/api/operations/journey", params={"patient_code": "PT-9999"})
    assert unknown.status_code == 400


async def test_the_bed_routes_are_read_only(client):
    for method in (client.post, client.put, client.delete):
        assert (await method("/api/operations/beds")).status_code == 405


async def test_trace_endpoint_empty_for_unknown_session(client):
    response = await client.get("/api/sessions/no-such-session/trace")
    assert response.status_code == 200
    assert response.json() == []


async def test_trace_and_messages_populated_after_a_chat_turn(client):
    chat_response = await client.post("/api/chat", json={"text": "list departments"})
    session_id = chat_response.json()["session_id"]

    messages_response = await client.get(f"/api/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert len(messages) == 2  # user turn + assistant reply
    assert messages[0]["role"] == "USER"
    assert messages[1]["role"] == "ASSISTANT"

    # The deterministic path never invokes the agent, so no AgentEvent rows
    # are expected here — this just confirms the endpoint works and returns
    # an empty trace rather than erroring for a session with no agent
    # activity yet.
    trace_response = await client.get(f"/api/sessions/{session_id}/trace")
    assert trace_response.status_code == 200
    assert trace_response.json() == []

"""Optional human confirmation of the agent's reschedule
(REQUIRE_RESCHEDULE_CONFIRMATION): proposing changes nothing, the confirm and
cancel routes resolve the proposal, and the chat response lists what is
still pending. With the flag off the tool is unchanged (test_audit_log.py)."""

import uuid
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.agent.grounding import GroundingRegistry
from app.agent.tools.action_tools import (
    RescheduleAppointmentArgs,
    handle_reschedule_appointment,
)
from app.api.routes.chat import get_checkpointer
from app.db.models.agent import AgentEvent, ConversationMessage
from app.db.models.hospital import Appointment, Scanner, ScannerStatus
from app.db.repositories.session_repository import SessionRepository
from app.db.session import get_db, get_session_factory
from app.main import app
from tests.integration.test_agent_loop import ScriptedChatModel, _settings
from tests.integration.test_audit_log import _max_audit_id, _movable_appointment, _new_records

pytestmark = pytest.mark.integration


def _flag(enabled: bool):
    return patch(
        "app.agent.tools.action_tools.get_settings",
        return_value=_settings(require_reschedule_confirmation=enabled),
    )


async def _propose(session):
    """Runs the tool with confirmation on, in a fresh session: returns
    (session_id, tool result, appointment code, scanner code)."""
    appointment, scanner = await _movable_appointment(session)
    session_id = str(uuid.uuid4())
    await SessionRepository(session).get_or_create(session_id)
    grounding = GroundingRegistry(session)
    await grounding.expose(session_id, "appointment", [appointment.code])
    await grounding.expose(session_id, "scanner", [scanner.code])
    with _flag(True):
        result = await handle_reschedule_appointment(
            session,
            session_id,
            RescheduleAppointmentArgs(appointment_code=appointment.code, scanner_code=scanner.code),
        )
    await session.commit()  # ToolTurn commits the tool call; a failed command rolls back the rest
    return session_id, result, appointment.code, scanner.code


async def _scanner_code_of(session, appointment_code: str) -> str:
    appointment = (
        await session.execute(select(Appointment).where(Appointment.code == appointment_code))
    ).scalar_one()
    await session.refresh(appointment, ["scanner"])
    return appointment.scanner.code


async def _event_types(session, session_id: str) -> list[str]:
    rows = await session.execute(
        select(AgentEvent.event_type).where(AgentEvent.session_id == session_id).order_by(AgentEvent.id)
    )
    return list(rows.scalars())


async def test_proposing_records_a_pending_proposal_and_changes_nothing(seeded_session, client):
    since = await _max_audit_id(seeded_session)
    appointment, _ = await _movable_appointment(seeded_session)
    before = await _scanner_code_of(seeded_session, appointment.code)

    session_id, result, appointment_code, scanner_code = await _propose(seeded_session)

    assert result["status"] == "pending_confirmation"
    assert "NOT been changed" in result["message"]
    assert appointment_code == appointment.code
    assert await _scanner_code_of(seeded_session, appointment_code) == before
    assert await _new_records(seeded_session, appointment_code, since) == []
    pending = (await client.get(f"/api/sessions/{session_id}/pending-reschedules")).json()
    assert pending == [
        {
            "proposal_id": result["proposal_id"],
            "appointment_code": appointment_code,
            "scanner_code": scanner_code,
            "new_start": None,
        }
    ]


async def test_confirm_applies_the_move_audits_it_and_records_the_exchange(seeded_session, client):
    since = await _max_audit_id(seeded_session)
    session_id, result, appointment_code, scanner_code = await _propose(seeded_session)
    url = f"/api/sessions/{session_id}/reschedule/{result['proposal_id']}/confirm"

    response = await client.post(url)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"
    assert appointment_code in body["message"] and scanner_code in body["message"]
    assert body["touched_entity_codes"] == [appointment_code, scanner_code]
    assert await _scanner_code_of(seeded_session, appointment_code) == scanner_code
    (record,) = await _new_records(seeded_session, appointment_code, since)
    assert record.actor == f"user_confirmed:{session_id}"
    assert await _event_types(seeded_session, session_id) == [
        "reschedule_proposed",
        "reschedule_confirmed",
    ]
    contents = (
        await seeded_session.execute(
            select(ConversationMessage.content)
            .where(ConversationMessage.session_id == session_id)
            .order_by(ConversationMessage.id)
        )
    ).scalars()
    assert list(contents) == ["Confirmed the proposed move.", body["message"]]
    assert (await client.get(f"/api/sessions/{session_id}/pending-reschedules")).json() == []

    assert (await client.post(url)).status_code == 409


async def test_cancel_leaves_the_appointment_alone(seeded_session, client):
    since = await _max_audit_id(seeded_session)
    appointment, _ = await _movable_appointment(seeded_session)
    before = await _scanner_code_of(seeded_session, appointment.code)
    session_id, result, appointment_code, _ = await _propose(seeded_session)

    response = await client.post(
        f"/api/sessions/{session_id}/reschedule/{result['proposal_id']}/cancel"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert await _scanner_code_of(seeded_session, appointment_code) == before
    assert await _new_records(seeded_session, appointment_code, since) == []
    assert (await _event_types(seeded_session, session_id))[-1] == "reschedule_cancelled"
    assert (await client.get(f"/api/sessions/{session_id}/pending-reschedules")).json() == []


async def test_another_sessions_proposal_is_not_found(seeded_session, client):
    _, result, _, _ = await _propose(seeded_session)
    other = str(uuid.uuid4())
    await SessionRepository(seeded_session).get_or_create(other)

    for action in ("confirm", "cancel"):
        response = await client.post(
            f"/api/sessions/{other}/reschedule/{result['proposal_id']}/{action}"
        )
        assert response.status_code == 404


async def test_confirm_that_breaks_a_business_rule_is_409_and_stays_pending(seeded_session, client):
    session_id, result, _, scanner_code = await _propose(seeded_session)
    scanner = (
        await seeded_session.execute(select(Scanner).where(Scanner.code == scanner_code))
    ).scalar_one()
    scanner.status = ScannerStatus.IN_USE
    await seeded_session.flush()

    response = await client.post(
        f"/api/sessions/{session_id}/reschedule/{result['proposal_id']}/confirm"
    )

    assert response.status_code == 409
    assert response.json()["detail"]
    pending = (await client.get(f"/api/sessions/{session_id}/pending-reschedules")).json()
    assert [p["proposal_id"] for p in pending] == [result["proposal_id"]]


@pytest.mark.parametrize(("enabled", "expected_pending"), [(True, 1), (False, 0)])
async def test_chat_response_lists_pending_confirmations_only_when_enabled(
    agent_session_factory, enabled, expected_pending
):
    def call(name, args, call_id):
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
        )

    model = ScriptedChatModel(
        responses=[
            call("search_appointments", {"appointment_type": "MRI", "status": "DELAYED"}, "c1"),
            call(
                "reschedule_appointment",
                {"appointment_code": "APT-2001", "scanner_code": "SCN-2"},
                "c2",
            ),
            AIMessage(content="Done."),
        ]
    )
    settings = _settings(
        require_reschedule_confirmation=enabled,
        enable_jev_fast_path=False,
        agent_runtime="in_process",
    )

    async def _db():
        async with agent_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_session_factory] = lambda: agent_session_factory
    app.dependency_overrides[get_checkpointer] = lambda: None
    try:
        with (
            patch("app.api.routes.chat.get_default_chat_model", return_value=model),
            patch("app.api.routes.chat.get_settings", return_value=settings),
            patch("app.agent.tools.action_tools.get_settings", return_value=settings),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as http:
                response = await http.post(
                    "/api/chat",
                    json={
                        "text": "Find the first delayed MRI appointment and move it to SCN-2",
                        "session_id": str(uuid.uuid4()),
                    },
                )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "agent"
    pending = body["pending_confirmations"]
    assert len(pending) == expected_pending
    if enabled:
        assert pending[0]["appointment_code"] == "APT-2001"
        assert pending[0]["scanner_code"] == "SCN-2"

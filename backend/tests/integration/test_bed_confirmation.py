"""Optional human confirmation of the agent's assign_bed / release_bed
(REQUIRE_BED_CONFIRMATION): proposing changes nothing, the confirm and cancel
routes resolve the proposal. With the flag off the tools act immediately."""

from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.agent.grounding import GroundingRegistry, GroundingRejectedError
from app.agent.tools.action_tools import (
    AssignBedArgs,
    ReleaseBedArgs,
    handle_assign_bed,
    handle_release_bed,
)
from app.db.models.agent import AgentEvent
from tests.integration.test_agent_loop import _settings
from tests.integration.test_wards_and_queue import (
    _bed_records,
    _max_audit_id,
    _run,
    _session_id,
)

pytestmark = pytest.mark.integration


def _flag(enabled: bool):
    return patch(
        "app.agent.tools.action_tools.get_settings",
        return_value=_settings(require_bed_confirmation=enabled),
    )


async def _bed_status(session, bed_code: str) -> str:
    beds = (await _run(session, "list_beds", {})).data
    return next(b["status"] for b in beds if b["code"] == bed_code)


async def _propose_assign(session) -> tuple[str, dict]:
    session_id = await _session_id(session)
    grounding = GroundingRegistry(session)
    await grounding.expose(session_id, "patient", ["PT-1029"])
    await grounding.expose(session_id, "bed", ["BED-2"])
    with _flag(True):
        result = await handle_assign_bed(
            session, session_id, AssignBedArgs(patient_code="PT-1029", bed_code="BED-2")
        )
    await session.commit()
    return session_id, result


async def _propose_release(session) -> tuple[str, dict]:
    session_id = await _session_id(session)
    await GroundingRegistry(session).expose(session_id, "bed", ["BED-1"])
    with _flag(True):
        result = await handle_release_bed(session, session_id, ReleaseBedArgs(bed_code="BED-1"))
    await session.commit()
    return session_id, result


async def _event_types(session, session_id: str) -> list[str]:
    rows = await session.execute(
        select(AgentEvent.event_type).where(AgentEvent.session_id == session_id).order_by(AgentEvent.id)
    )
    return list(rows.scalars())


async def test_flag_off_acts_immediately(seeded_session):
    session_id = await _session_id(seeded_session)
    grounding = GroundingRegistry(seeded_session)
    await grounding.expose(session_id, "patient", ["PT-1029"])
    await grounding.expose(session_id, "bed", ["BED-2"])

    with _flag(False):
        bed = await handle_assign_bed(
            seeded_session, session_id, AssignBedArgs(patient_code="PT-1029", bed_code="BED-2")
        )

    assert bed["status"] == "OCCUPIED"
    assert await _event_types(seeded_session, session_id) == []


async def test_proposing_assign_records_a_proposal_and_changes_nothing(seeded_session, client):
    since = await _max_audit_id(seeded_session)

    session_id, result = await _propose_assign(seeded_session)

    assert result["status"] == "pending_confirmation"
    assert "NOT been changed" in result["message"]
    assert await _bed_status(seeded_session, "BED-2") == "AVAILABLE"
    assert await _bed_records(seeded_session, "BED-2", since) == []
    assert await _event_types(seeded_session, session_id) == ["bed_action_proposed"]
    pending = (await client.get(f"/api/sessions/{session_id}/pending-bed-actions")).json()
    assert pending == [
        {
            "proposal_id": result["proposal_id"],
            "action": "assign_bed",
            "bed_code": "BED-2",
            "patient_code": "PT-1029",
        }
    ]


async def test_proposing_release_lists_it_without_a_patient(seeded_session, client):
    session_id, result = await _propose_release(seeded_session)

    assert result["status"] == "pending_confirmation"
    assert await _bed_status(seeded_session, "BED-1") == "OCCUPIED"
    pending = (await client.get(f"/api/sessions/{session_id}/pending-bed-actions")).json()
    assert pending == [
        {
            "proposal_id": result["proposal_id"],
            "action": "release_bed",
            "bed_code": "BED-1",
            "patient_code": None,
        }
    ]


async def test_confirm_assign_applies_it_audits_it_and_resolves_the_proposal(seeded_session, client):
    since = await _max_audit_id(seeded_session)
    session_id, result = await _propose_assign(seeded_session)
    url = f"/api/sessions/{session_id}/bed-actions/{result['proposal_id']}/confirm"

    response = await client.post(url)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"
    assert "PT-1029" in body["message"] and "BED-2" in body["message"]
    assert body["touched_entity_codes"] == ["BED-2", "PT-1029"]
    assert await _bed_status(seeded_session, "BED-2") == "OCCUPIED"
    (record,) = await _bed_records(seeded_session, "BED-2", since)
    assert record.actor == f"user_confirmed:{session_id}"
    assert await _event_types(seeded_session, session_id) == [
        "bed_action_proposed",
        "bed_action_confirmed",
    ]
    assert (await client.get(f"/api/sessions/{session_id}/pending-bed-actions")).json() == []

    assert (await client.post(url)).status_code == 409


async def test_confirm_release_applies_it(seeded_session, client):
    session_id, result = await _propose_release(seeded_session)

    response = await client.post(
        f"/api/sessions/{session_id}/bed-actions/{result['proposal_id']}/confirm"
    )

    assert response.status_code == 200
    assert response.json()["touched_entity_codes"] == ["BED-1"]
    assert await _bed_status(seeded_session, "BED-1") == "CLEANING"


async def test_cancel_leaves_the_bed_alone(seeded_session, client):
    since = await _max_audit_id(seeded_session)
    session_id, result = await _propose_assign(seeded_session)
    url = f"/api/sessions/{session_id}/bed-actions/{result['proposal_id']}/cancel"

    response = await client.post(url)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert await _bed_status(seeded_session, "BED-2") == "AVAILABLE"
    assert await _bed_records(seeded_session, "BED-2", since) == []
    assert (await _event_types(seeded_session, session_id))[-1] == "bed_action_cancelled"
    assert (await client.get(f"/api/sessions/{session_id}/pending-bed-actions")).json() == []
    assert (await client.post(url)).status_code == 409


async def test_unknown_proposal_is_404_for_both_actions(seeded_session, client):
    session_id, result = await _propose_assign(seeded_session)
    other = await _session_id(seeded_session)

    for session, proposal_id in ((session_id, 999999999), (other, result["proposal_id"])):
        for action in ("confirm", "cancel"):
            response = await client.post(
                f"/api/sessions/{session}/bed-actions/{proposal_id}/{action}"
            )
            assert response.status_code == 404


async def test_ungrounded_codes_are_rejected_before_any_proposal(seeded_session):
    session_id = await _session_id(seeded_session)

    with _flag(True):
        with pytest.raises(GroundingRejectedError):
            await handle_assign_bed(
                seeded_session, session_id, AssignBedArgs(patient_code="PT-1029", bed_code="BED-2")
            )
        with pytest.raises(GroundingRejectedError):
            await handle_release_bed(seeded_session, session_id, ReleaseBedArgs(bed_code="BED-1"))

    assert await _event_types(seeded_session, session_id) == []

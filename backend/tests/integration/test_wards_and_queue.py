"""Wards, beds, the emergency queue and the patient journey: the read commands,
the two audited bed writes (rules, grounding, audit), and the agent tools.

Seed reference (QUEUE_PLAN in app/seed/seed_data.py): PT-1027 holds BED-1
(ER-1) and PT-1028 holds BED-5 (ER-2). BED-2 and BED-4 are AVAILABLE, BED-3 is
CLEANING. PT-1029 (ER-3, CRITICAL, not yet triaged), PT-1030 (ER-4) and
PT-1008 (ER-5) are WAITING."""

import uuid

import pytest
from sqlalchemy import func, select

from app.agent.grounding import GroundingRegistry, GroundingRejectedError
from app.agent.tools import TOOL_REGISTRY
from app.agent.tools.action_tools import (
    AssignBedArgs,
    ReleaseBedArgs,
    handle_assign_bed,
    handle_release_bed,
)
from app.agent.tools.base import ToolExecutionError
from app.agent.tools.search_tools import (
    GetPatientJourneyArgs,
    SearchBedsArgs,
    SearchQueueArgs,
    handle_get_patient_journey,
    handle_search_beds,
    handle_search_queue,
)
from app.db.models.audit import AuditRecord
from app.db.models.hospital import BedStatus
from app.db.repositories.audit_repository import AuditRepository
from app.db.repositories.ward_repository import BedRepository
from app.db.repositories.grounding_repository import GroundingRepository
from app.db.repositories.session_repository import SessionRepository
from app.execution.commands import Command, CommandRunner
from app.execution.commands.base import is_audited

pytestmark = pytest.mark.integration


async def _run(session, name, args=None):
    return await CommandRunner(session, actor="agent_session:test").execute(Command(name, args or {}))


async def _session_id(session) -> str:
    session_id = f"test-beds-{uuid.uuid4().hex[:8]}"
    await SessionRepository(session).get_or_create(session_id)
    return session_id


async def _max_audit_id(session) -> int:
    return (await session.execute(select(func.coalesce(func.max(AuditRecord.id), 0)))).scalar_one()


async def _bed_records(session, bed_code: str, since_id: int) -> list[AuditRecord]:
    records = await AuditRepository(session).list_for_entity("bed", bed_code)
    return [r for r in records if r.id > since_id]


def test_only_the_two_bed_writes_are_audited_and_flagged_as_writes():
    assert is_audited("assign_bed") and is_audited("release_bed")
    assert not is_audited("list_beds") and not is_audited("patient_journey")
    for name in ("assign_bed", "release_bed"):
        assert TOOL_REGISTRY[name].is_write is True
    for name in ("search_beds", "search_queue", "get_patient_journey"):
        assert TOOL_REGISTRY[name].is_write is False


# ---- reads ---------------------------------------------------------------


async def test_list_wards_counts_beds(seeded_session):
    result = await _run(seeded_session, "list_wards")
    assert result.success
    wards = {w["code"]: w for w in result.data}
    assert set(wards) == {"WRD-1", "WRD-2", "WRD-3"}
    assert wards["WRD-1"]["department"] == {"code": "DEPT-ER", "name": "Emergency"}
    assert (wards["WRD-1"]["bed_count"], wards["WRD-1"]["occupied_count"]) == (4, 1)


async def test_list_beds_filters_and_shape(seeded_session):
    result = await _run(seeded_session, "list_beds", {"status": "AVAILABLE", "ward_code": "WRD-1"})
    assert [b["code"] for b in result.data] == ["BED-2", "BED-4"]
    assert result.data[0] == {
        "code": "BED-2",
        "ward": {"code": "WRD-1", "name": "Emergency Bay"},
        "status": "AVAILABLE",
        "patient": None,
        "occupied_since": None,
    }
    occupied = (await _run(seeded_session, "list_beds", {"status": "OCCUPIED"})).data
    assert occupied[0]["patient"].keys() == {"code", "name", "mrn"}
    assert occupied[0]["occupied_since"] is not None


async def test_list_beds_rejects_an_unknown_status(seeded_session):
    result = await _run(seeded_session, "list_beds", {"status": "BROKEN"})
    assert not result.success and result.error_category == "invalid_argument"


async def test_queue_is_most_urgent_first_and_filterable(seeded_session):
    result = await _run(seeded_session, "list_emergency_queue")
    urgencies = [e["urgency"] for e in result.data]
    assert urgencies == sorted(urgencies, key=["CRITICAL", "URGENT", "STANDARD"].index)
    entry = next(e for e in result.data if e["code"] == "ER-1")
    assert entry["bed"] == {"code": "BED-1", "ward": {"code": "WRD-1", "name": "Emergency Bay"}}
    assert entry["patient"].keys() == {"code", "name"}
    assert entry["wait_minutes"] == 0  # admitted: no longer waiting
    er3 = next(e for e in result.data if e["code"] == "ER-3")
    assert er3["wait_minutes"] >= 0 and er3["status"] == "WAITING"

    waiting = (await _run(seeded_session, "list_emergency_queue", {"status": "WAITING"})).data
    assert {e["code"] for e in waiting} == {"ER-3", "ER-4", "ER-5"}
    critical = (await _run(seeded_session, "list_emergency_queue", {"urgency": "CRITICAL"})).data
    assert {e["code"] for e in critical} == {"ER-1", "ER-3"}


async def test_journey_of_an_admitted_patient(seeded_session):
    admitted = (await _run(seeded_session, "list_emergency_queue", {"status": "ADMITTED"})).data
    entry = next(e for e in admitted if e["code"] == "ER-2")
    result = await _run(seeded_session, "patient_journey", {"patient_code": entry["patient"]["code"]})
    journey = result.data
    assert [s["stage"] for s in journey["stages"]] == [
        "ARRIVED", "TRIAGED", "BED_ASSIGNED", "SCAN", "DISCHARGED",
    ]
    assert journey["current_stage"] == "BED_ASSIGNED"
    by_stage = {s["stage"]: s for s in journey["stages"]}
    assert by_stage["BED_ASSIGNED"]["detail"] == "BED-5 (Cardiac Ward)"
    assert by_stage["DISCHARGED"]["at"] is None
    assert {s["stage"]: s["reached"] for s in journey["stages"]}["BED_ASSIGNED"] is True
    assert by_stage["DISCHARGED"]["reached"] is False
    assert isinstance(journey["appointments"], list)


async def test_journey_by_name_and_for_a_patient_without_a_queue_entry(seeded_session):
    by_code = (await _run(seeded_session, "patient_journey", {"patient_code": "PT-1029"})).data
    by_name = (await _run(seeded_session, "patient_journey", {"query": by_code["patient"]["name"]})).data
    assert by_name == by_code
    assert by_code["current_stage"] == "ARRIVED"

    none = (await _run(seeded_session, "patient_journey", {"patient_code": "PT-1013"})).data
    assert none["stages"] == [] and none["current_stage"] == "NONE"


async def test_journey_errors(seeded_session):
    assert (await _run(seeded_session, "patient_journey", {})).error_category == "invalid_argument"
    assert (await _run(seeded_session, "patient_journey", {"query": "Zzyzx"})).error_category == "not_found"
    assert (await _run(seeded_session, "patient_journey", {"query": "a"})).error_category == "ambiguous_patient"


# ---- writes: business rules ------------------------------------------------


async def test_assign_bed_admits_the_waiting_patient(seeded_session):
    result = await _run(seeded_session, "assign_bed", {"patient_code": "PT-1029", "bed_code": "BED-2"})
    assert result.success
    assert result.data["status"] == "OCCUPIED" and result.data["patient"]["code"] == "PT-1029"
    assert result.data["occupied_since"] is not None

    queue = (await _run(seeded_session, "list_emergency_queue", {"status": "ADMITTED"})).data
    entry = next(e for e in queue if e["code"] == "ER-3")
    assert entry["bed"]["code"] == "BED-2"
    journey = (await _run(seeded_session, "patient_journey", {"patient_code": "PT-1029"})).data
    assert journey["current_stage"] == "BED_ASSIGNED"


@pytest.mark.parametrize(
    "patient, bed, category",
    [
        ("PT-1029", "BED-1", "bed_unavailable"),
        ("PT-1029", "BED-3", "bed_unavailable"),
        ("PT-1027", "BED-2", "already_bedded"),
        ("PT-1013", "BED-2", "no_waiting_entry"),
        (None, "BED-2", "invalid_argument"),
    ],
)
async def test_assign_bed_rule_violations(seeded_session, patient, bed, category):
    args = {"bed_code": bed} if patient is None else {"patient_code": patient, "bed_code": bed}
    result = await _run(seeded_session, "assign_bed", args)
    assert not result.success and result.error_category == category


async def test_assign_bed_unknown_codes_are_not_found(seeded_session):
    for args in (
        {"patient_code": "PT-9999", "bed_code": "BED-2"},
        {"patient_code": "PT-1029", "bed_code": "BED-99"},
    ):
        assert (await _run(seeded_session, "assign_bed", args)).error_category == "not_found"


async def test_release_bed_discharges_and_marks_for_cleaning(seeded_session):
    result = await _run(seeded_session, "release_bed", {"bed_code": "BED-1"})
    assert result.success
    assert result.data["status"] == "CLEANING"
    assert result.data["patient"] is None and result.data["occupied_since"] is None

    discharged = (await _run(seeded_session, "list_emergency_queue", {"status": "DISCHARGED"})).data
    entry = next(e for e in discharged if e["code"] == "ER-1")
    assert entry["discharged_at"] is not None
    journey = (await _run(seeded_session, "patient_journey", {"patient_code": entry["patient"]["code"]})).data
    assert journey["current_stage"] == "DISCHARGED"
    by_stage = {s["stage"]: s for s in journey["stages"]}
    assert by_stage["BED_ASSIGNED"]["reached"] is True and by_stage["BED_ASSIGNED"]["at"] is None
    assert by_stage["DISCHARGED"]["reached"] is True
    assert entry["wait_minutes"] == 0


async def test_release_bed_without_an_admitted_entry_is_a_failed_result(seeded_session):
    bed = await BedRepository(seeded_session).get_by_code("BED-2")
    bed.status = BedStatus.OCCUPIED
    await seeded_session.flush()

    result = await _run(seeded_session, "release_bed", {"bed_code": "BED-2"})
    assert not result.success and result.error_category == "bed_not_occupied"


async def test_release_bed_rule_violations(seeded_session):
    assert (await _run(seeded_session, "release_bed", {"bed_code": "BED-2"})).error_category == "bed_not_occupied"
    assert (await _run(seeded_session, "release_bed", {})).error_category == "invalid_argument"
    assert (await _run(seeded_session, "release_bed", {"bed_code": "BED-99"})).error_category == "not_found"


# ---- writes: audit ----------------------------------------------------------


async def test_assign_and_release_each_write_one_audit_record_with_codes_only(seeded_session):
    since = await _max_audit_id(seeded_session)
    patient = (await _run(seeded_session, "patient_journey", {"patient_code": "PT-1029"})).data["patient"]
    await _run(seeded_session, "assign_bed", {"patient_code": "PT-1029", "bed_code": "BED-2"})
    await _run(seeded_session, "release_bed", {"bed_code": "BED-2"})

    records = await _bed_records(seeded_session, "BED-2", since)
    assert [r.action for r in records] == ["assign_bed", "release_bed"]
    assign, release = records
    assert assign.actor == "agent_session:test"
    assert assign.before_json == {
        "ward_code": "WRD-1",
        "bed_status": "AVAILABLE",
        "patient_code": None,
        "occupied_since": None,
        "queue_entry_code": "ER-3",
        "queue_status": "WAITING",
    }
    assert assign.after_json.keys() == assign.before_json.keys()
    assert assign.after_json["occupied_since"] is not None
    assert {k: v for k, v in assign.after_json.items() if k != "occupied_since"} == {
        "ward_code": "WRD-1",
        "bed_status": "OCCUPIED",
        "patient_code": "PT-1029",
        "queue_entry_code": "ER-3",
        "queue_status": "ADMITTED",
    }
    assert release.before_json["occupied_since"] == assign.after_json["occupied_since"]
    assert release.after_json["occupied_since"] is None
    assert release.after_json["bed_status"] == "CLEANING"
    assert release.after_json["queue_status"] == "DISCHARGED"
    assert patient["name"] not in repr([(r.before_json, r.after_json) for r in records])


async def test_a_rejected_bed_write_writes_no_audit_record(seeded_session):
    since = await _max_audit_id(seeded_session)
    await _run(seeded_session, "assign_bed", {"patient_code": "PT-1029", "bed_code": "BED-1"})
    await _run(seeded_session, "release_bed", {"bed_code": "BED-2"})
    assert await _max_audit_id(seeded_session) == since


# ---- agent tools -------------------------------------------------------------


async def test_search_tools_ground_what_they_return(seeded_session):
    session_id = await _session_id(seeded_session)
    repo = GroundingRepository(seeded_session)

    beds = await handle_search_beds(seeded_session, session_id, SearchBedsArgs(status="AVAILABLE"))
    queue = await handle_search_queue(seeded_session, session_id, SearchQueueArgs(status="WAITING"))
    journey = await handle_get_patient_journey(
        seeded_session, session_id, GetPatientJourneyArgs(patient_code="PT-1027")
    )

    assert len(beds) == 6 and len(queue) == 3
    assert set(await repo.list_grounded(session_id, "bed")) == {b["code"] for b in beds}
    assert set(await repo.list_grounded(session_id, "queue_entry")) == {"ER-3", "ER-4", "ER-5"}
    assert {"PT-1027", "PT-1029"} <= set(await repo.list_grounded(session_id, "patient"))
    assert journey["patient"]["code"] == "PT-1027"


async def test_assign_bed_tool_rejects_ungrounded_codes_and_changes_nothing(seeded_session):
    session_id = await _session_id(seeded_session)
    since = await _max_audit_id(seeded_session)
    args = AssignBedArgs(patient_code="PT-1029", bed_code="BED-2")

    with pytest.raises(GroundingRejectedError):
        await handle_assign_bed(seeded_session, session_id, args)
    await GroundingRegistry(seeded_session).expose(session_id, "patient", ["PT-1029"])
    with pytest.raises(GroundingRejectedError):
        await handle_assign_bed(seeded_session, session_id, args)

    assert await _max_audit_id(seeded_session) == since
    available = (await _run(seeded_session, "list_beds", {"status": "AVAILABLE"})).data
    assert "BED-2" in [b["code"] for b in available]


async def test_assign_then_release_through_the_tools(seeded_session):
    session_id = await _session_id(seeded_session)
    since = await _max_audit_id(seeded_session)
    await handle_search_queue(seeded_session, session_id, SearchQueueArgs(status="WAITING"))
    await handle_search_beds(seeded_session, session_id, SearchBedsArgs())

    bed = await handle_assign_bed(
        seeded_session, session_id, AssignBedArgs(patient_code="PT-1029", bed_code="BED-2")
    )
    assert bed["status"] == "OCCUPIED"
    bed = await handle_release_bed(seeded_session, session_id, ReleaseBedArgs(bed_code="BED-2"))
    assert bed["status"] == "CLEANING"

    records = await _bed_records(seeded_session, "BED-2", since)
    assert [r.actor for r in records] == [f"agent_session:{session_id}"] * 2


async def test_release_bed_tool_rejects_an_ungrounded_bed(seeded_session):
    session_id = await _session_id(seeded_session)
    with pytest.raises(GroundingRejectedError):
        await handle_release_bed(seeded_session, session_id, ReleaseBedArgs(bed_code="BED-1"))


async def test_tools_surface_command_errors(seeded_session):
    session_id = await _session_id(seeded_session)
    await GroundingRegistry(seeded_session).expose(session_id, "bed", ["BED-2"])
    with pytest.raises(ToolExecutionError) as caught:
        await handle_release_bed(seeded_session, session_id, ReleaseBedArgs(bed_code="BED-2"))
    assert caught.value.category == "bed_not_occupied"

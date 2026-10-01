"""The mutation audit log (observability Phase 6): who / what / before /
after for every successful reschedule, written atomically with the change,
nothing for a rejected one, and append-only at the database level."""

import uuid

import pytest
import structlog
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import selectinload

from app.agent.grounding import GroundingRegistry
from app.agent.tools.action_tools import (
    RescheduleAppointmentArgs,
    handle_reschedule_appointment,
)
from app.db.models.audit import AuditRecord
from app.db.models.hospital import Appointment, Patient, Scanner, ScannerStatus
from app.db.repositories.audit_repository import AuditRepository
from app.db.repositories.session_repository import SessionRepository
from app.execution.commands import Command, CommandRunner
from app.execution.commands.base import UNATTRIBUTED_ACTOR, is_audited

pytestmark = pytest.mark.integration


async def _movable_appointment(session) -> tuple[Appointment, Scanner]:
    """An appointment plus a different, AVAILABLE scanner of its modality.
    Callers copy what they need into plain values before executing a
    command: a rollback expires these instances, and an async session can't
    lazy-load them afterwards."""
    appointments = (
        await session.execute(
            select(Appointment).options(selectinload(Appointment.scanner)).order_by(Appointment.id)
        )
    ).scalars()
    scanners = list(
        (
            await session.execute(
                select(Scanner).where(Scanner.status == ScannerStatus.AVAILABLE)
            )
        ).scalars()
    )
    for appointment in appointments:
        for scanner in scanners:
            if scanner.type == appointment.appointment_type and scanner.id != appointment.scanner_id:
                return appointment, scanner
    raise AssertionError("seed data has no movable appointment")


async def _max_audit_id(session) -> int:
    return (await session.execute(select(func.coalesce(func.max(AuditRecord.id), 0)))).scalar_one()


async def _new_records(session, appointment_code: str, since_id: int) -> list[AuditRecord]:
    """Only the records this test wrote. Other tests (the agent-loop ones)
    commit real reschedules to the test database, and audit rows can't be
    deleted, so older rows for the same appointment code persist across
    runs."""
    records = await AuditRepository(session).list_for_entity("appointment", appointment_code)
    return [r for r in records if r.id > since_id]


def test_reassign_scanner_is_the_audited_command():
    assert is_audited("reassign_scanner")
    assert not is_audited("search_appointments")


async def test_reschedule_tool_writes_one_complete_audit_record(seeded_session):
    since = await _max_audit_id(seeded_session)
    appointment, scanner = await _movable_appointment(seeded_session)
    old_scanner_code = appointment.scanner.code if appointment.scanner else None
    session_id = f"test-audit-{uuid.uuid4().hex[:8]}"
    await SessionRepository(seeded_session).get_or_create(session_id)
    grounding = GroundingRegistry(seeded_session)
    await grounding.expose(session_id, "appointment", [appointment.code])
    await grounding.expose(session_id, "scanner", [scanner.code])

    with structlog.contextvars.bound_contextvars(request_id="req-audit-1"):
        await handle_reschedule_appointment(
            seeded_session,
            session_id,
            RescheduleAppointmentArgs(appointment_code=appointment.code, scanner_code=scanner.code),
        )

    records = await _new_records(seeded_session, appointment.code, since)
    assert len(records) == 1
    record = records[0]
    assert record.actor == f"agent_session:{session_id}"
    assert record.action == "reassign_scanner"
    assert record.before_json["scanner_code"] == old_scanner_code
    assert record.after_json["scanner_code"] == scanner.code
    assert record.after_json["status"] == "SCHEDULED"
    assert set(record.before_json) == {"scanner_code", "scheduled_start", "scheduled_end", "status"}
    assert record.request_id == "req-audit-1"
    assert record.trace_id is None  # tracing is off in tests


async def test_audit_record_carries_no_patient_data(seeded_session):
    since = await _max_audit_id(seeded_session)
    appointment, scanner = await _movable_appointment(seeded_session)
    patient = await seeded_session.get(Patient, appointment.patient_id)
    await CommandRunner(seeded_session, actor="agent_session:x").execute(
        Command(
            "reassign_scanner",
            {"appointment_code": appointment.code, "scanner_code": scanner.code},
        )
    )
    (record,) = await _new_records(seeded_session, appointment.code, since)
    stored = repr((record.before_json, record.after_json, record.actor))
    for value in (patient.name, patient.mrn, patient.code):
        assert value not in stored


async def test_rejected_reschedule_writes_no_audit_record(seeded_session):
    since = await _max_audit_id(seeded_session)
    appointment, _ = await _movable_appointment(seeded_session)
    appointment_code = appointment.code
    unavailable = (
        await seeded_session.execute(
            select(Scanner).where(Scanner.status != ScannerStatus.AVAILABLE).limit(1)
        )
    ).scalar_one()
    result = await CommandRunner(seeded_session, actor="agent_session:x").execute(
        Command(
            "reassign_scanner",
            {"appointment_code": appointment_code, "scanner_code": unavailable.code},
        )
    )
    assert not result.success
    assert await _new_records(seeded_session, appointment_code, since) == []


async def test_a_caller_without_an_actor_is_still_audited(seeded_session):
    """A future entry point that forgets to say who it is must not produce
    an unaudited mutation."""
    since = await _max_audit_id(seeded_session)
    appointment, scanner = await _movable_appointment(seeded_session)
    result = await CommandRunner(seeded_session).execute(
        Command(
            "reassign_scanner",
            {"appointment_code": appointment.code, "scanner_code": scanner.code},
        )
    )
    assert result.success
    records = await _new_records(seeded_session, appointment.code, since)
    assert [r.actor for r in records] == [UNATTRIBUTED_ACTOR]


@pytest.mark.parametrize("statement", ["UPDATE audit_log SET actor = 'someone_else'", "DELETE FROM audit_log"])
async def test_audit_log_is_append_only_in_the_database(seeded_session, statement):
    appointment, scanner = await _movable_appointment(seeded_session)
    await CommandRunner(seeded_session, actor="agent_session:x").execute(
        Command(
            "reassign_scanner",
            {"appointment_code": appointment.code, "scanner_code": scanner.code},
        )
    )
    assert (await seeded_session.execute(select(AuditRecord))).scalars().first() is not None
    with pytest.raises(DBAPIError, match="append-only"):
        async with seeded_session.begin_nested():
            await seeded_session.execute(text(statement))

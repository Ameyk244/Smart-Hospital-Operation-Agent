"""Tests for the trusted execution path — CommandRunner + command handlers —
with no parser and no LLM involved (concept 54's other half: "executor
without the parser", proving the runner is independently correct). Uses a
real Postgres database, per docs/ARCHITECTURE.md's mocks-are-for-unit-tests-
only rule.
"""

import pytest

from app.execution.commands import Command, CommandRunner

pytestmark = pytest.mark.integration


async def test_unknown_command_is_rejected_before_touching_the_db(db_session):
    runner = CommandRunner(db_session)
    result = await runner.execute(Command("not_a_real_command", {}))
    assert result.success is False
    assert result.error_category == "unknown_command"


async def test_list_departments(seeded_session):
    runner = CommandRunner(seeded_session)
    result = await runner.execute(Command("list_departments"))
    assert result.success is True
    assert len(result.data) == 4


async def test_search_patients_returns_multiple_plausible_matches(seeded_session):
    runner = CommandRunner(seeded_session)
    result = await runner.execute(Command("search_patients", {"query": "a"}))
    assert result.success is True
    assert len(result.data) > 1


async def test_search_patients_requires_query(seeded_session):
    runner = CommandRunner(seeded_session)
    result = await runner.execute(Command("search_patients", {}))
    assert result.success is False
    assert result.error_category == "invalid_argument"


async def test_list_delayed_appointments_mri(seeded_session):
    runner = CommandRunner(seeded_session)
    result = await runner.execute(
        Command("list_delayed_appointments", {"appointment_type": "MRI"})
    )
    assert result.success is True
    assert len(result.data) >= 2
    assert all(a["status"] == "DELAYED" for a in result.data)
    assert all(a["appointment_type"] == "MRI" for a in result.data)


async def test_reassign_scanner_happy_path(seeded_session):
    runner = CommandRunner(seeded_session)
    delayed = await runner.execute(
        Command("list_delayed_appointments", {"appointment_type": "MRI"})
    )
    target = delayed.data[0]
    scanners = await runner.execute(Command("list_scanners", {"type": "MRI", "status": "AVAILABLE"}))
    # Pick an available scanner different from whatever this appointment
    # already has, so the test actually exercises a change.
    new_scanner = next(s for s in scanners.data if s["code"] != target["scanner"]["code"])

    result = await runner.execute(
        Command(
            "reassign_scanner",
            {"appointment_code": target["code"], "scanner_code": new_scanner["code"]},
        )
    )
    assert result.success is True
    assert result.data["scanner"]["code"] == new_scanner["code"]
    assert result.data["status"] == "SCHEDULED"


async def test_reassign_scanner_rejects_modality_mismatch(seeded_session):
    runner = CommandRunner(seeded_session)
    delayed = await runner.execute(
        Command("list_delayed_appointments", {"appointment_type": "MRI"})
    )
    target = delayed.data[0]
    ct_scanners = await runner.execute(Command("list_scanners", {"type": "CT"}))
    wrong_scanner = ct_scanners.data[0]

    result = await runner.execute(
        Command(
            "reassign_scanner",
            {"appointment_code": target["code"], "scanner_code": wrong_scanner["code"]},
        )
    )
    assert result.success is False
    assert result.error_category == "modality_mismatch"


async def test_reassign_scanner_rejects_unavailable_scanner(seeded_session):
    runner = CommandRunner(seeded_session)
    delayed = await runner.execute(
        Command("list_delayed_appointments", {"appointment_type": "MRI"})
    )
    target = delayed.data[0]
    busy_scanners = await runner.execute(
        Command("list_scanners", {"type": "MRI", "status": "IN_USE"})
    )
    assert busy_scanners.data, "seed data must include an IN_USE MRI scanner for this test"
    busy_scanner = busy_scanners.data[0]

    result = await runner.execute(
        Command(
            "reassign_scanner",
            {"appointment_code": target["code"], "scanner_code": busy_scanner["code"]},
        )
    )
    assert result.success is False
    assert result.error_category == "scanner_unavailable"


async def test_reassign_scanner_unknown_appointment_code(seeded_session):
    runner = CommandRunner(seeded_session)
    result = await runner.execute(
        Command("reassign_scanner", {"appointment_code": "APT-9999", "scanner_code": "SCN-1"})
    )
    assert result.success is False
    assert result.error_category == "not_found"


async def test_reassign_scanner_is_transactional_on_failure(seeded_session):
    """A rejected reassignment must leave the appointment completely
    unchanged — proving CommandRunner rolls back rather than partially
    applying a failed command."""
    runner = CommandRunner(seeded_session)
    delayed = await runner.execute(
        Command("list_delayed_appointments", {"appointment_type": "MRI"})
    )
    target = delayed.data[0]
    original_scanner_code = target["scanner"]["code"]

    ct_scanners = await runner.execute(Command("list_scanners", {"type": "CT"}))
    await runner.execute(
        Command(
            "reassign_scanner",
            {"appointment_code": target["code"], "scanner_code": ct_scanners.data[0]["code"]},
        )
    )

    reloaded = await runner.execute(
        Command("search_appointments", {"patient_code": target["patient"]["code"]})
    )
    reloaded_target = next(a for a in reloaded.data if a["code"] == target["code"])
    assert reloaded_target["scanner"]["code"] == original_scanner_code

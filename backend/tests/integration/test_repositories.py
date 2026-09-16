"""Integration tests for the repository layer, against a real Postgres
database (see tests/conftest.py). No mocks: these prove the SQLAlchemy
queries are actually correct, not just that a fake returns what we told it
to.
"""

from datetime import datetime, timezone

import pytest

from app.db.models.hospital import AppointmentStatus, ScannerStatus, ScannerType
from app.db.repositories.appointment_repository import AppointmentRepository
from app.db.repositories.grounding_repository import GroundingRepository
from app.db.repositories.patient_repository import PatientRepository
from app.db.repositories.preference_repository import PreferenceRepository
from app.db.repositories.scanner_repository import ScannerRepository
from app.db.repositories.session_repository import SessionRepository
from app.db.models.agent import MessageRole

pytestmark = pytest.mark.integration


async def test_seed_produces_expected_counts(seeded_session):
    scanners = await ScannerRepository(seeded_session).search()
    patients = await PatientRepository(seeded_session).search_by_name("", limit=100)
    assert len(scanners) == 8
    assert len(patients) == 30


async def test_delayed_mri_appointments_have_multiple_results(seeded_session):
    repo = AppointmentRepository(seeded_session)
    results = await repo.search(
        status=AppointmentStatus.DELAYED, appointment_type=ScannerType.MRI
    )
    # Grounding-by-rejection only means something if search legitimately
    # returns more than one candidate.
    assert len(results) >= 2
    codes = {a.code for a in results}
    assert len(codes) == len(results)


async def test_compatible_available_scanners_for_mri_has_multiple_candidates(seeded_session):
    repo = ScannerRepository(seeded_session)
    candidates = await repo.list_compatible_available(ScannerType.MRI)
    assert len(candidates) >= 2
    assert all(s.type == ScannerType.MRI for s in candidates)
    assert all(s.status == ScannerStatus.AVAILABLE for s in candidates)


async def test_patient_fuzzy_search_is_case_insensitive(seeded_session):
    repo = PatientRepository(seeded_session)
    results_lower = await repo.search_by_name("james")
    results_title = await repo.search_by_name("James")
    assert [p.code for p in results_lower] == [p.code for p in results_title]


async def test_reassign_scanner_updates_row(seeded_session):
    appt_repo = AppointmentRepository(seeded_session)
    scanner_repo = ScannerRepository(seeded_session)

    delayed = await appt_repo.search(
        status=AppointmentStatus.DELAYED, appointment_type=ScannerType.MRI, limit=1
    )
    target = delayed[0]
    new_scanner = (await scanner_repo.list_compatible_available(ScannerType.MRI))[0]
    new_start = datetime.now(timezone.utc)

    updated = await appt_repo.reassign_scanner(
        target.code,
        new_scanner_id=new_scanner.id,
        new_start=new_start,
        new_end=new_start,
    )
    assert updated.scanner_id == new_scanner.id
    assert updated.status == AppointmentStatus.SCHEDULED

    reloaded = await appt_repo.get_by_code(target.code)
    assert reloaded.scanner.code == new_scanner.code


async def test_reassign_unknown_appointment_raises(seeded_session):
    appt_repo = AppointmentRepository(seeded_session)
    with pytest.raises(ValueError):
        await appt_repo.reassign_scanner(
            "APT-9999", new_scanner_id=1, new_start=datetime.now(timezone.utc), new_end=datetime.now(timezone.utc)
        )


async def test_preference_remember_forget_roundtrip(db_session):
    session_repo = SessionRepository(db_session)
    await session_repo.get_or_create("sess-1")
    pref_repo = PreferenceRepository(db_session)

    await pref_repo.remember("sess-1", "preferred_scanner_type", "MRI")
    prefs = await pref_repo.list_for_session("sess-1")
    assert {p.key: p.value for p in prefs} == {"preferred_scanner_type": "MRI"}

    # Remembering the same key again overwrites, doesn't duplicate.
    await pref_repo.remember("sess-1", "preferred_scanner_type", "CT")
    prefs = await pref_repo.list_for_session("sess-1")
    assert len(prefs) == 1
    assert prefs[0].value == "CT"

    removed = await pref_repo.forget("sess-1", "preferred_scanner_type")
    assert removed is True
    assert await pref_repo.list_for_session("sess-1") == []


async def test_grounding_expose_and_check(db_session):
    session_repo = SessionRepository(db_session)
    await session_repo.get_or_create("sess-2")
    grounding_repo = GroundingRepository(db_session)

    await grounding_repo.expose("sess-2", "appointment", ["APT-2001", "APT-2002"])

    assert await grounding_repo.is_grounded("sess-2", "appointment", "APT-2001") is True
    assert await grounding_repo.is_grounded("sess-2", "appointment", "APT-9999") is False
    assert set(await grounding_repo.list_grounded("sess-2", "appointment")) == {
        "APT-2001",
        "APT-2002",
    }


async def test_conversation_messages_ordered_oldest_first(db_session):
    session_repo = SessionRepository(db_session)
    await session_repo.get_or_create("sess-3")
    await session_repo.append_message("sess-3", MessageRole.USER, "first")
    await session_repo.append_message("sess-3", MessageRole.ASSISTANT, "second")

    messages = await session_repo.get_recent_messages("sess-3")
    assert [m.content for m in messages] == ["first", "second"]

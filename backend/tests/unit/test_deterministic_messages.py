"""Reply text for the ward, bed, queue and journey commands (the parser path)."""

from app.api.routes.chat import _format_deterministic_message
from app.execution.commands import Command


def _msg(name: str, data: object) -> str:
    return _format_deterministic_message(Command(name), True, None, data)


def test_wards_show_occupancy():
    data = [{"name": "Cardiac Ward", "occupied_count": 1, "bed_count": 3}]
    assert _msg("list_wards", data) == "Found 1 ward: Cardiac Ward (1/3 beds occupied)."


def test_beds_list_code_ward_and_status():
    data = [{"code": "BED-2", "ward": {"name": "Emergency Bay"}, "status": "AVAILABLE"}]
    assert _msg("list_beds", data) == "Found 1 bed: BED-2 (Emergency Bay, AVAILABLE)."


def test_queue_lists_patient_urgency_and_status():
    data = [{"code": "ER-3", "patient": {"name": "Wei Gonzalez"}, "urgency": "CRITICAL", "status": "WAITING"}]
    assert _msg("list_emergency_queue", data) == "Found 1 queue entry: ER-3 Wei Gonzalez (CRITICAL, WAITING)."


def test_journey_names_the_current_stage():
    data = {"patient": {"name": "John Anderson"}, "current_stage": "BED_ASSIGNED"}
    assert _msg("patient_journey", data) == "John Anderson: current stage BED_ASSIGNED."


def test_empty_results_say_so():
    assert _msg("list_wards", []) == "No wards found."
    assert _msg("list_beds", []) == "No beds matched that filter."
    assert _msg("list_emergency_queue", []) == "The emergency queue is empty."

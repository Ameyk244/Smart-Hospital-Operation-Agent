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
    assert _msg("list_emergency_queue", data) == "Found 1 patient in the emergency queue: ER-3 Wei Gonzalez (CRITICAL, WAITING)."


def test_journey_names_the_current_stage():
    data = {
        "patient": {"name": "John Anderson"},
        "stages": [{"stage": "ARRIVED"}],
        "current_stage": "BED_ASSIGNED",
    }
    assert _msg("patient_journey", data) == "John Anderson: current stage BED_ASSIGNED."


def test_journey_without_an_emergency_visit_says_so():
    data = {"patient": {"name": "John Anderson"}, "stages": [], "current_stage": "NONE"}
    assert _msg("patient_journey", data) == "John Anderson has no emergency visit."


def test_empty_results_say_so():
    assert _msg("list_wards", []) == "No wards found."
    assert _msg("list_beds", []) == "No beds matched that filter."
    assert _msg("list_emergency_queue", []) == "The emergency queue is empty."


def test_watch_and_alert_replies():
    watch = {"code": "TSK-2", "condition": "FREE_BEDS_UNDER", "threshold": 2, "scope_code": "WRD-1", "active": True}
    assert _msg("create_watch", watch) == "Watch TSK-2 started: FREE_BEDS_UNDER 2 in WRD-1."
    assert _msg("list_watches", [watch, {**watch, "code": "TSK-3", "active": False}]) == (
        "Watches: TSK-2 FREE_BEDS_UNDER 2, TSK-3 FREE_BEDS_UNDER 2 (stopped)."
    )
    assert _msg("list_watches", []) == "No watches."
    assert _msg("stop_watch", watch) == "Watch TSK-2 stopped."
    assert _msg("list_alerts", []) == "No open alerts."
    alert = {"id": 5, "severity": "WARNING", "summary": "Only 1 free beds in the hospital (limit 2)"}
    assert _msg("list_alerts", [alert]) == "1 open alert: #5 WARNING: Only 1 free beds in the hospital (limit 2)."
    assert _msg("acknowledge_alert", alert) == "Alert 5 acknowledged."
    assert _msg("acknowledge_all_alerts", {"acknowledged": 2}) == "Acknowledged 2 alerts."

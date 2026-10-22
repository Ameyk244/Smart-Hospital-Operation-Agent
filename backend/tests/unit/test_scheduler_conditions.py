"""The pure watch conditions: fire, don't fire, boundaries, per-entity keys."""

from app.db.models.scheduler import AlertSeverity
from app.scheduler.conditions import (
    delayed_appointments_over,
    free_beds_under,
    queue_wait_over,
)


def _entry(code, wait, urgency="URGENT", status="WAITING"):
    return {"code": code, "wait_minutes": wait, "urgency": urgency, "status": status}


def test_queue_wait_fires_only_above_the_threshold_and_per_entry():
    breaches = queue_wait_over([_entry("ER-1", 31), _entry("ER-2", 30), _entry("ER-3", 90, "CRITICAL")], 30)

    assert [b.dedupe_key for b in breaches] == ["ER-1", "ER-3"]
    assert [b.entity_codes for b in breaches] == [["ER-1"], ["ER-3"]]
    assert [b.severity for b in breaches] == [AlertSeverity.WARNING, AlertSeverity.CRITICAL]
    assert breaches[0].summary == "ER-1 (URGENT) has waited 31 min (limit 30)"


def test_queue_wait_ignores_entries_that_are_not_waiting():
    assert queue_wait_over([_entry("ER-1", 99, status="ADMITTED")], 30) == []
    assert queue_wait_over([], 30) == []


def test_free_beds_fires_below_the_threshold_only():
    beds = [{"code": "BED-1"}, {"code": "BED-2"}]

    assert free_beds_under(beds, 2) == []
    [breach] = free_beds_under(beds, 3)
    assert (breach.dedupe_key, breach.severity, breach.entity_codes) == ("hospital", AlertSeverity.WARNING, [])
    assert breach.summary == "Only 2 free beds in the hospital (limit 3)"


def test_summaries_use_the_singular_for_one():
    assert free_beds_under([{"code": "BED-1"}], 2)[0].summary == "Only 1 free bed in the hospital (limit 2)"
    assert delayed_appointments_over([{"code": "APT-1"}], 0)[0].summary == "1 delayed appointment (limit 0)"


def test_free_beds_is_critical_at_zero_and_keyed_by_ward_when_scoped():
    [breach] = free_beds_under([], 1, "WRD-2")

    assert (breach.dedupe_key, breach.severity, breach.entity_codes) == ("WRD-2", AlertSeverity.CRITICAL, ["WRD-2"])
    assert free_beds_under([], 0, "WRD-2") == []


def test_delayed_appointments_fire_above_the_threshold_only():
    appointments = [{"code": "APT-1"}, {"code": "APT-2"}, {"code": "APT-3"}]

    assert delayed_appointments_over(appointments, 3) == []
    [breach] = delayed_appointments_over(appointments, 2)
    assert (breach.dedupe_key, breach.entity_codes) == ("hospital", ["APT-1", "APT-2", "APT-3"])
    assert breach.summary == "3 delayed appointments (limit 2)"

"""The watch conditions: one pure function per condition over the result of
one existing read command (docs/SCHEDULER.md section 4). Each returns a
`Breach` per alert it would raise, keyed so the runner can tell "still the
same problem" from a new one."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.db.models.scheduler import AlertSeverity, WatchCondition

HOSPITAL_KEY = "hospital"


@dataclass(frozen=True)
class Breach:
    dedupe_key: str
    severity: AlertSeverity
    summary: str
    entity_codes: list[str]


@dataclass(frozen=True)
class ConditionSpec:
    command: str
    command_args: Callable[[str | None], dict[str, Any]]
    evaluate: Callable[[list[dict], int, str | None], list[Breach]]


def queue_wait_over(entries: list[dict], threshold: int, _scope: str | None = None) -> list[Breach]:
    return [
        Breach(
            e["code"],
            AlertSeverity.CRITICAL if e["urgency"] == "CRITICAL" else AlertSeverity.WARNING,
            f"{e['code']} ({e['urgency']}) has waited {e['wait_minutes']} min (limit {threshold})",
            [e["code"]],
        )
        for e in entries
        if e["status"] == "WAITING" and e["wait_minutes"] > threshold
    ]


def free_beds_under(beds: list[dict], threshold: int, scope: str | None = None) -> list[Breach]:
    free = len(beds)
    if free >= threshold:
        return []
    where = scope or "the hospital"
    return [
        Breach(
            scope or HOSPITAL_KEY,
            AlertSeverity.CRITICAL if free == 0 else AlertSeverity.WARNING,
            f"Only {free} free bed{'s' * (free != 1)} in {where} (limit {threshold})",
            [scope] if scope else [],
        )
    ]


def delayed_appointments_over(
    appointments: list[dict], threshold: int, _scope: str | None = None
) -> list[Breach]:
    if len(appointments) <= threshold:
        return []
    return [
        Breach(
            HOSPITAL_KEY,
            AlertSeverity.WARNING,
            f"{len(appointments)} delayed appointment{'s' * (len(appointments) != 1)} (limit {threshold})",
            [a["code"] for a in appointments],
        )
    ]


CONDITIONS: dict[WatchCondition, ConditionSpec] = {
    WatchCondition.QUEUE_WAIT_OVER: ConditionSpec(
        "list_emergency_queue", lambda _scope: {"status": "WAITING"}, queue_wait_over
    ),
    WatchCondition.FREE_BEDS_UNDER: ConditionSpec(
        "list_beds", lambda scope: {"status": "AVAILABLE", "ward_code": scope}, free_beds_under
    ),
    WatchCondition.DELAYED_APPOINTMENTS_OVER: ConditionSpec(
        "search_appointments",
        lambda _scope: {"status": "DELAYED", "limit": 1000},
        delayed_appointments_over,
    ),
}

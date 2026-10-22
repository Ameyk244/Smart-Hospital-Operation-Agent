"""Watches and alerts: commands, the runner tick and the read-only HTTP routes.
Time is injected into `run_tick`, so nothing sleeps."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db.base import utcnow
from app.db.models.audit import AuditRecord
from app.db.models.scheduler import Alert, ScheduledTask, WatchCondition
from app.execution.commands import Command, CommandRunner
from app.execution.commands.base import is_audited
from app.execution.commands.scheduler_commands import MAX_ACTIVE_WATCHES
from app.main import app  # noqa: F401  imported at collection, not inside the client fixture
from app.scheduler import runner
from app.scheduler.conditions import CONDITIONS, ConditionSpec

pytestmark = pytest.mark.integration

LATER = timedelta(days=1)


@pytest.fixture(autouse=True)
def scheduler_on(monkeypatch):
    settings = get_settings().model_copy(update={"scheduler_enabled": True})
    for module in ("app.execution.commands.scheduler_commands", "app.api.routes.operations"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)


def _Factory(session):
    """Runner sessions that share the test's connection, so everything the
    runner commits stays inside the transaction rolled back at the end."""
    return async_sessionmaker(
        bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


async def _run(session, name, args=None):
    return await CommandRunner(session, actor="tester").execute(Command(name, args or {}))


async def _watch(session, condition, threshold, **extra):
    result = await _run(session, "create_watch", {"condition": condition, "threshold": threshold, **extra})
    assert result.success, result.error
    return result.data


async def _alerts(session, status="ALL"):
    return (await _run(session, "list_alerts", {"status": status})).data


async def test_create_list_and_stop_a_watch_with_audit(seeded_session):
    task = await _watch(seeded_session, "FREE_BEDS_UNDER", 2, ward_code="WRD-1")

    assert task["code"].startswith("TSK-")
    assert (task["condition"], task["threshold"], task["scope_code"]) == ("FREE_BEDS_UNDER", 2, "WRD-1")
    assert task["period_seconds"] == 300 and task["active"] is True
    assert [w["code"] for w in (await _run(seeded_session, "list_watches")).data] == [task["code"]]

    stopped = await _run(seeded_session, "stop_watch", {"task_code": task["code"]})
    assert stopped.data["active"] is False
    again = await _run(seeded_session, "stop_watch", {"task_code": task["code"]})
    assert again.error_category == "already_stopped"

    records = (await seeded_session.scalars(select(AuditRecord).where(AuditRecord.entity_code == task["code"]))).all()
    assert [(r.action, r.actor) for r in records] == [("create_watch", "tester"), ("stop_watch", "tester")]
    assert all(is_audited(n) for n in ("create_watch", "stop_watch", "acknowledge_alert", "acknowledge_all_alerts"))
    assert not is_audited("list_watches") and not is_audited("list_alerts")


@pytest.mark.parametrize(
    "args, category",
    [
        ({"condition": "NOPE", "threshold": 1}, "invalid_argument"),
        ({"condition": "QUEUE_WAIT_OVER"}, "invalid_argument"),
        ({"condition": "QUEUE_WAIT_OVER", "threshold": 0}, "invalid_argument"),
        ({"condition": "QUEUE_WAIT_OVER", "threshold": 1, "ward_code": "WRD-1"}, "invalid_argument"),
        ({"condition": "FREE_BEDS_UNDER", "threshold": 1, "ward_code": "WRD-999"}, "not_found"),
    ],
)
async def test_create_watch_rejects_bad_arguments(seeded_session, args, category):
    result = await _run(seeded_session, "create_watch", args)

    assert (result.success, result.error_category) == (False, category)
    assert await seeded_session.scalar(select(func.count()).select_from(ScheduledTask)) == 0


async def test_create_watch_rejects_an_identical_active_watch_until_it_is_stopped(seeded_session):
    first = await _watch(seeded_session, "FREE_BEDS_UNDER", 2, ward_code="WRD-1")

    dup = await _run(seeded_session, "create_watch", {"condition": "FREE_BEDS_UNDER", "threshold": 2, "ward_code": "WRD-1"})
    assert (dup.success, dup.error_category) == (False, "invalid_argument") and first["code"] in dup.error
    await _watch(seeded_session, "FREE_BEDS_UNDER", 2)

    await _run(seeded_session, "stop_watch", {"task_code": first["code"]})
    await _watch(seeded_session, "FREE_BEDS_UNDER", 2, ward_code="WRD-1")


async def test_create_watch_caps_the_active_watches(seeded_session):
    for threshold in range(1, MAX_ACTIVE_WATCHES + 1):
        await _watch(seeded_session, "QUEUE_WAIT_OVER", threshold)

    result = await _run(seeded_session, "create_watch", {"condition": "QUEUE_WAIT_OVER", "threshold": 99})

    assert (result.success, result.error_category) == (False, "invalid_argument")
    assert str(MAX_ACTIVE_WATCHES) in result.error


async def test_stopping_a_watch_acknowledges_its_open_alerts(seeded_session):
    stopped = await _watch(seeded_session, "FREE_BEDS_UNDER", 1000)
    await _watch(seeded_session, "DELAYED_APPOINTMENTS_OVER", 1)
    await runner.run_tick(_Factory(seeded_session), utcnow() + LATER)
    assert len(await _alerts(seeded_session, "OPEN")) == 2

    await _run(seeded_session, "stop_watch", {"task_code": stopped["code"]})

    [open_alert] = await _alerts(seeded_session, "OPEN")
    assert open_alert["task_code"] != stopped["code"]


async def test_unknown_codes_are_not_found(seeded_session):
    assert (await _run(seeded_session, "stop_watch", {"task_code": "TSK-999"})).error_category == "not_found"
    assert (await _run(seeded_session, "acknowledge_alert", {"alert_id": 999})).error_category == "not_found"


async def test_commands_say_the_scheduler_is_off(seeded_session, monkeypatch):
    off = get_settings().model_copy(update={"scheduler_enabled": False})
    monkeypatch.setattr("app.execution.commands.scheduler_commands.get_settings", lambda: off)

    for name, args in [
        ("create_watch", {"condition": "FREE_BEDS_UNDER", "threshold": 1}),
        ("list_watches", {}),
        ("stop_watch", {"task_code": "TSK-1"}),
        ("list_alerts", {}),
        ("acknowledge_alert", {"alert_id": 1}),
        ("acknowledge_all_alerts", {}),
    ]:
        result = await _run(seeded_session, name, args)
        assert (result.success, result.error_category) == (False, "scheduler_off")


async def test_a_tick_raises_one_alert_and_dedupes_while_it_is_open(seeded_session):
    task = await _watch(seeded_session, "FREE_BEDS_UNDER", 1000)
    factory = _Factory(seeded_session)
    now = utcnow() + LATER

    await runner.run_tick(factory, now)
    await runner.run_tick(factory, now + timedelta(seconds=task["period_seconds"] + 1))

    [alert] = await _alerts(seeded_session)
    assert (alert["task_code"], alert["severity"], alert["acknowledged_at"]) == (task["code"], "WARNING", None)
    assert alert["summary"].startswith("Only ") and alert["entity_codes"] == []
    [watch] = (await _run(seeded_session, "list_watches")).data
    assert watch["last_run_at"] is not None


async def test_a_task_that_is_not_due_does_not_run(seeded_session):
    await _watch(seeded_session, "FREE_BEDS_UNDER", 1000)

    await runner.run_tick(_Factory(seeded_session), utcnow() - LATER)

    assert await _alerts(seeded_session) == []


async def test_a_stopped_task_does_not_run(seeded_session):
    task = await _watch(seeded_session, "FREE_BEDS_UNDER", 1000)
    await _run(seeded_session, "stop_watch", {"task_code": task["code"]})

    await runner.run_tick(_Factory(seeded_session), utcnow() + LATER)

    assert await _alerts(seeded_session) == []


async def test_alerts_again_after_acknowledgement(seeded_session):
    task = await _watch(seeded_session, "DELAYED_APPOINTMENTS_OVER", 1)
    factory = _Factory(seeded_session)
    now = utcnow() + LATER
    await runner.run_tick(factory, now)
    [first] = await _alerts(seeded_session)
    assert first["entity_codes"]

    acked = await _run(seeded_session, "acknowledge_alert", {"alert_id": first["id"]})
    assert acked.data["acknowledged_at"] is not None
    again = await _run(seeded_session, "acknowledge_alert", {"alert_id": first["id"]})
    assert again.error_category == "already_acknowledged"
    assert await _alerts(seeded_session, "OPEN") == []

    await runner.run_tick(factory, now + timedelta(seconds=task["period_seconds"] + 1))
    assert len(await _alerts(seeded_session)) == 2
    assert len(await _alerts(seeded_session, "OPEN")) == 1


async def test_acknowledge_all_closes_every_open_alert(seeded_session):
    await _watch(seeded_session, "FREE_BEDS_UNDER", 1000)
    await _watch(seeded_session, "DELAYED_APPOINTMENTS_OVER", 1)
    await runner.run_tick(_Factory(seeded_session), utcnow() + LATER)

    result = await _run(seeded_session, "acknowledge_all_alerts")

    assert result.data == {"acknowledged": 2}
    assert await _alerts(seeded_session, "OPEN") == []
    assert await seeded_session.scalar(select(func.count()).select_from(Alert)) == 2


async def test_a_failing_task_is_isolated_and_still_rescheduled(seeded_session, monkeypatch):
    broken = ConditionSpec("no_such_command", lambda _scope: {}, lambda *_: [])
    monkeypatch.setitem(CONDITIONS, WatchCondition.QUEUE_WAIT_OVER, broken)
    failing = await _watch(seeded_session, "QUEUE_WAIT_OVER", 1)
    await _watch(seeded_session, "FREE_BEDS_UNDER", 1000)
    now = utcnow() + LATER

    await runner.run_tick(_Factory(seeded_session), now)

    assert len(await _alerts(seeded_session)) == 1
    task = await seeded_session.scalar(select(ScheduledTask).where(ScheduledTask.code == failing["code"]))
    assert task.last_run_at == now and task.next_run_at == now + timedelta(seconds=task.period_seconds)


async def test_queue_watch_raises_an_alert_per_waiting_entry(seeded_session):
    await _watch(seeded_session, "QUEUE_WAIT_OVER", 1)
    await runner.run_tick(_Factory(seeded_session), utcnow() + LATER)

    waiting = (await _run(seeded_session, "list_emergency_queue", {"status": "WAITING"})).data
    alerts = await _alerts(seeded_session)
    assert waiting and sorted(a["entity_codes"][0] for a in alerts) == sorted(e["code"] for e in waiting)


async def test_http_shapes_and_status_filter(client, seeded_session):
    task = await _watch(seeded_session, "FREE_BEDS_UNDER", 1000)
    await runner.run_tick(_Factory(seeded_session), utcnow() + LATER)

    tasks = (await client.get("/api/operations/scheduled-tasks")).json()
    assert tasks[0].keys() == {"code", "condition", "threshold", "scope_code", "period_seconds", "active", "last_run_at"}
    assert tasks[0]["code"] == task["code"]

    [alert] = (await client.get("/api/operations/alerts")).json()
    assert alert.keys() == {"id", "task_code", "severity", "summary", "entity_codes", "created_at", "acknowledged_at"}

    await _run(seeded_session, "acknowledge_all_alerts")
    assert (await client.get("/api/operations/alerts")).json() == []
    assert len((await client.get("/api/operations/alerts?status=all")).json()) == 1
    assert (await client.get("/api/operations/alerts?status=bogus")).status_code == 422


async def test_http_routes_are_empty_when_the_scheduler_is_off(client, seeded_session, monkeypatch):
    await _watch(seeded_session, "FREE_BEDS_UNDER", 1000)
    off = get_settings().model_copy(update={"scheduler_enabled": False})
    monkeypatch.setattr("app.api.routes.operations.get_settings", lambda: off)

    assert (await client.get("/api/operations/alerts")).json() == []
    assert (await client.get("/api/operations/scheduled-tasks")).json() == []

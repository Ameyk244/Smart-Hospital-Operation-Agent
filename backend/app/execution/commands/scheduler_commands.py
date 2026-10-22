"""Watch and alert commands (docs/SCHEDULER.md).

Why it exists: backs "watch ...", "list watches", "stop watch", "list alerts"
and "acknowledge ..." in the deterministic grammar. The four writes touch only
`scheduled_tasks` and `alerts`, never hospital data, and are audited with codes
and booleans only. With `SCHEDULER_ENABLED` off every command answers that the
scheduler is off. The agent has no tool for these; its `execute_command` refuses
audited commands.
"""

from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.base import utcnow
from app.db.models.hospital import Ward
from app.db.models.scheduler import Alert, ScheduledTask, WatchCondition
from app.db.repositories.audit_repository import AuditRepository
from app.execution.commands.base import CommandError, register
from app.execution.commands.coercion import coerce_watch_condition

DEFAULT_PERIOD_SECONDS = 300
MAX_ACTIVE_WATCHES = 20


def _require_enabled() -> None:
    if not get_settings().scheduler_enabled:
        raise CommandError("The scheduler is off", category="scheduler_off")


def _task_dict(task: ScheduledTask) -> dict[str, Any]:
    return {
        "code": task.code,
        "condition": task.condition.value,
        "threshold": task.threshold,
        "scope_code": task.scope_code,
        "period_seconds": task.period_seconds,
        "active": task.active,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
    }


def _alert_dict(alert: Alert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "task_code": alert.task.code,
        "severity": alert.severity.value,
        "summary": alert.summary,
        "entity_codes": alert.entity_codes,
        "created_at": alert.created_at.isoformat(),
        "acknowledged_at": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
    }


def _alerts_query():
    return (
        select(Alert)
        .options(selectinload(Alert.task))
        .execution_options(populate_existing=True)
    )


async def _audit(
    session: AsyncSession, action: str, entity_type: str, entity_code: str,
    before: dict, after: dict, actor: str,
) -> None:
    await AuditRepository(session).record(
        actor=actor, action=action, entity_type=entity_type, entity_code=entity_code,
        before=before, after=after,
    )


@register("create_watch", audited=True)
async def create_watch(session: AsyncSession, args: dict[str, Any], *, actor: str) -> dict:
    _require_enabled()
    condition = coerce_watch_condition(args.get("condition"))
    threshold = args.get("threshold")
    ward_code = args.get("ward_code")
    if condition is None or not isinstance(threshold, int) or threshold < 1:
        raise CommandError(
            "condition and an integer threshold of at least 1 are required", category="invalid_argument"
        )
    if ward_code is not None:
        if condition != WatchCondition.FREE_BEDS_UNDER:
            raise CommandError("Only a free-beds watch can be limited to a ward", category="invalid_argument")
        if await session.scalar(select(Ward.id).where(Ward.code == ward_code)) is None:
            raise ValueError(f"No ward with code {ward_code!r}")
    active = (await session.scalars(select(ScheduledTask).where(ScheduledTask.active))).all()
    if len(active) >= MAX_ACTIVE_WATCHES:
        raise CommandError(f"At most {MAX_ACTIVE_WATCHES} watches can be active", category="invalid_argument")
    for t in active:
        if (t.condition, t.threshold, t.scope_code) == (condition, threshold, ward_code):
            raise CommandError(f"Watch {t.code} already does this", category="invalid_argument")

    task_id = await session.scalar(text("SELECT nextval(pg_get_serial_sequence('scheduled_tasks', 'id'))"))
    now = utcnow()
    task = ScheduledTask(
        id=task_id,
        code=f"TSK-{task_id}",
        condition=condition,
        threshold=threshold,
        scope_code=ward_code,
        period_seconds=DEFAULT_PERIOD_SECONDS,
        next_run_at=now,
    )
    session.add(task)
    await session.flush()
    await _audit(
        session, "create_watch", "scheduled_task", task.code,
        {}, {"condition": condition.value, "threshold": threshold, "scope_code": ward_code}, actor,
    )
    return _task_dict(task)


@register("list_watches")
async def list_watches(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    _require_enabled()
    tasks = await session.scalars(
        select(ScheduledTask).order_by(ScheduledTask.id).execution_options(populate_existing=True)
    )
    return [_task_dict(t) for t in tasks]


@register("stop_watch", audited=True)
async def stop_watch(session: AsyncSession, args: dict[str, Any], *, actor: str) -> dict:
    _require_enabled()
    code = args.get("task_code")
    if not code:
        raise CommandError("task_code is required", category="invalid_argument")
    task = await session.scalar(
        select(ScheduledTask).where(ScheduledTask.code == code).with_for_update()
    )
    if task is None:
        raise ValueError(f"No watch with code {code!r}")
    if not task.active:
        raise CommandError(f"Watch {code} is already stopped", category="already_stopped")
    task.active = False
    await session.execute(
        update(Alert)
        .where(Alert.task_id == task.id, Alert.acknowledged_at.is_(None))
        .values(acknowledged_at=utcnow())
    )
    await _audit(session, "stop_watch", "scheduled_task", code, {"active": True}, {"active": False}, actor)
    return _task_dict(task)


@register("list_alerts")
async def list_alerts(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    _require_enabled()
    stmt = _alerts_query().order_by(Alert.id.desc())
    if args.get("status", "OPEN") != "ALL":
        stmt = stmt.where(Alert.acknowledged_at.is_(None))
    return [_alert_dict(a) for a in await session.scalars(stmt)]


@register("acknowledge_alert", audited=True)
async def acknowledge_alert(session: AsyncSession, args: dict[str, Any], *, actor: str) -> dict:
    _require_enabled()
    alert_id = args.get("alert_id")
    if not isinstance(alert_id, int):
        raise CommandError("alert_id is required", category="invalid_argument")
    alert = await session.scalar(_alerts_query().where(Alert.id == alert_id).with_for_update(of=Alert))
    if alert is None:
        raise ValueError(f"No alert with id {alert_id}")
    if alert.acknowledged_at is not None:
        raise CommandError(f"Alert {alert_id} is already acknowledged", category="already_acknowledged")
    alert.acknowledged_at = utcnow()
    await _audit(
        session, "acknowledge_alert", "alert", f"ALERT-{alert_id}",
        {"acknowledged": False}, {"acknowledged": True}, actor,
    )
    return _alert_dict(alert)


@register("acknowledge_all_alerts", audited=True)
async def acknowledge_all_alerts(session: AsyncSession, args: dict[str, Any], *, actor: str) -> dict:
    _require_enabled()
    result = await session.execute(
        update(Alert).where(Alert.acknowledged_at.is_(None)).values(acknowledged_at=utcnow())
    )
    await _audit(
        session, "acknowledge_all_alerts", "alert", "ALL", {"open": result.rowcount}, {"open": 0}, actor
    )
    return {"acknowledged": result.rowcount}

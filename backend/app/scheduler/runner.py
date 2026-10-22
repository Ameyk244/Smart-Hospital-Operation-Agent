"""The watch runner: a background loop that checks due watches and raises
alerts (docs/SCHEDULER.md section 3). Read-only: each check is a read command
through `CommandRunner`, and the only rows it writes are alerts and the task's
own run times. No LLM is involved. Telemetry is optional; a failing task is
logged by id and never stops the loop."""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import utcnow
from app.db.models.scheduler import Alert, ScheduledTask
from app.execution.commands import Command, CommandRunner
from app.observability import metrics
from app.observability.logging_config import get_logger
from app.scheduler.conditions import CONDITIONS

logger = get_logger("scheduler")


async def _check(session: AsyncSession, factory: async_sessionmaker, task: ScheduledTask) -> bool:
    """Runs one watch; True when it raised a new alert. The read uses its own
    session because `CommandRunner` commits, which would release the row lock
    held on the due tasks."""
    spec = CONDITIONS[task.condition]
    async with factory() as read_session:
        result = await CommandRunner(read_session).execute(
            Command(spec.command, spec.command_args(task.scope_code))
        )
    if not result.success:
        raise RuntimeError(result.error_category)
    open_keys = set(
        (
            await session.scalars(
                select(Alert.dedupe_key).where(
                    Alert.task_id == task.id, Alert.acknowledged_at.is_(None)
                )
            )
        ).all()
    )
    raised = False
    for breach in spec.evaluate(result.data, task.threshold, task.scope_code):
        if breach.dedupe_key not in open_keys:
            session.add(
                Alert(
                    task_id=task.id,
                    dedupe_key=breach.dedupe_key,
                    severity=breach.severity,
                    summary=breach.summary,
                    entity_codes=breach.entity_codes,
                )
            )
            raised = True
    return raised


async def run_tick(factory: async_sessionmaker, now: datetime) -> None:
    async with factory() as session:
        due = await session.scalars(
            select(ScheduledTask)
            .where(ScheduledTask.active.is_(True), ScheduledTask.next_run_at <= now)
            .order_by(ScheduledTask.id)
            .with_for_update(skip_locked=True)
        )
        for task in due.all():
            status = "ok"
            try:
                async with session.begin_nested():
                    status = "alerted" if await _check(session, factory, task) else "ok"
            except Exception as exc:  # noqa: BLE001 - one task never stops the loop
                status = "failed"
                logger.warning("scheduler_task_failed", task_code=task.code, error=type(exc).__name__)
            metrics.record_scheduler_run(task.condition.value, status)
            task.last_run_at = now
            task.next_run_at = now + timedelta(seconds=task.period_seconds)
        await session.commit()


async def run_scheduler(factory: async_sessionmaker, tick_seconds: int) -> None:
    while True:
        try:
            await run_tick(factory, utcnow())
        except Exception as exc:  # noqa: BLE001 - e.g. the database is briefly down
            logger.warning("scheduler_tick_failed", error=type(exc).__name__)
        await asyncio.sleep(tick_seconds)

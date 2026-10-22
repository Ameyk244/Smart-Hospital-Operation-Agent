"""Watch and alert tools (docs/SCHEDULER.md). Each calls the scheduler command
of the same name through `CommandRunner`, so validation, caps, audit and the
scheduler-off error come from the command. A watch only monitors; it never
changes hospital data."""

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.grounding import GroundingRegistry
from app.agent.tools.base import ToolExecutionError, ToolSpec, register_tool
from app.execution.commands import Command, CommandRunner

MAX_LISTED_ALERTS = 50


async def _run(session: AsyncSession, session_id: str, name: str, args: dict) -> object:
    result = await CommandRunner(session, actor=f"agent_session:{session_id}").execute(Command(name, args))
    if not result.success:
        raise ToolExecutionError(result.error or f"{name} failed", result.error_category or "tool_error")
    return result.data


class NoArgs(BaseModel):
    pass


async def handle_list_watches(session: AsyncSession, session_id: str, args: NoArgs) -> object:
    watches = await _run(session, session_id, "list_watches", {})
    await GroundingRegistry(session).expose(session_id, "watch", [w["code"] for w in watches])
    return watches


LIST_WATCHES_TOOL = register_tool(
    ToolSpec(
        name="list_watches",
        description=(
            "List the monitoring watches with their condition, threshold and whether each is "
            "active. Only a task code returned by this tool may be passed to stop_watch."
        ),
        args_schema=NoArgs,
        handler=handle_list_watches,
        is_write=False,
        requires_setting="scheduler_enabled",
    )
)


class ListAlertsArgs(BaseModel):
    status: Literal["OPEN", "ALL"] = Field("OPEN", description="OPEN (default) or ALL")


async def handle_list_alerts(session: AsyncSession, session_id: str, args: ListAlertsArgs) -> object:
    alerts = await _run(session, session_id, "list_alerts", {"status": args.status, "limit": MAX_LISTED_ALERTS})
    await GroundingRegistry(session).expose(session_id, "alert", [str(a["id"]) for a in alerts])
    return alerts


LIST_ALERTS_TOOL = register_tool(
    ToolSpec(
        name="list_alerts",
        description=(
            "List alerts raised by watches, newest first. Only an alert id returned by this "
            "tool may be passed to acknowledge_alert."
        ),
        args_schema=ListAlertsArgs,
        handler=handle_list_alerts,
        is_write=False,
        requires_setting="scheduler_enabled",
    )
)


class CreateWatchArgs(BaseModel):
    condition: Literal["QUEUE_WAIT_OVER", "FREE_BEDS_UNDER", "DELAYED_APPOINTMENTS_OVER"] = Field(
        ...,
        description="QUEUE_WAIT_OVER (minutes a patient has waited), FREE_BEDS_UNDER (available "
        "beds) or DELAYED_APPOINTMENTS_OVER (count)",
    )
    threshold: int = Field(..., ge=1, le=999_999, description="The limit the condition is compared with")
    ward_code: str | None = Field(None, description="A ward code; only for FREE_BEDS_UNDER")


async def handle_create_watch(session: AsyncSession, session_id: str, args: CreateWatchArgs) -> object:
    watch = await _run(session, session_id, "create_watch", args.model_dump(exclude_none=True))
    await GroundingRegistry(session).expose(session_id, "watch", [watch["code"]])
    return watch


CREATE_WATCH_TOOL = register_tool(
    ToolSpec(
        name="create_watch",
        description=(
            "Start a read-only monitor that raises an alert when a condition is crossed. It "
            "only observes; it never changes beds, queue or appointments."
        ),
        args_schema=CreateWatchArgs,
        handler=handle_create_watch,
        is_write=True,
        requires_setting="scheduler_enabled",
    )
)


class StopWatchArgs(BaseModel):
    task_code: str = Field(..., description="A watch code previously returned by list_watches")


async def handle_stop_watch(session: AsyncSession, session_id: str, args: StopWatchArgs) -> object:
    await GroundingRegistry(session).require_grounded(session_id, "watch", args.task_code)
    return await _run(session, session_id, "stop_watch", {"task_code": args.task_code})


STOP_WATCH_TOOL = register_tool(
    ToolSpec(
        name="stop_watch",
        description=(
            "Stop an active watch and close its open alerts. task_code MUST come from a prior "
            "list_watches result in this conversation — inventing a code will be rejected."
        ),
        args_schema=StopWatchArgs,
        handler=handle_stop_watch,
        is_write=True,
        requires_setting="scheduler_enabled",
    )
)


class AcknowledgeAlertArgs(BaseModel):
    alert_id: int = Field(..., description="An alert id previously returned by list_alerts")


async def handle_acknowledge_alert(
    session: AsyncSession, session_id: str, args: AcknowledgeAlertArgs
) -> object:
    await GroundingRegistry(session).require_grounded(session_id, "alert", str(args.alert_id))
    return await _run(session, session_id, "acknowledge_alert", {"alert_id": args.alert_id})


ACKNOWLEDGE_ALERT_TOOL = register_tool(
    ToolSpec(
        name="acknowledge_alert",
        description=(
            "Mark one open alert as acknowledged. alert_id MUST come from a prior list_alerts "
            "result in this conversation — inventing an id will be rejected."
        ),
        args_schema=AcknowledgeAlertArgs,
        handler=handle_acknowledge_alert,
        is_write=True,
        requires_setting="scheduler_enabled",
    )
)

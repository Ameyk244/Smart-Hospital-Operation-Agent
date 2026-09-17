"""Action/write tools (docs/ARCHITECTURE.md §4, category 5).

Why it exists: `reschedule_appointment` is the agent-facing entry point for
moving an appointment to a (possibly new) scanner. It does not reimplement
that operation — it grounds its arguments, then builds the exact same
`Command("reassign_scanner", ...)` the deterministic path could build, and
hands it to the same `CommandRunner` (docs/ARCHITECTURE.md §2). Grounding is
checked *before* the command is even constructed: an appointment_code or
scanner_code the model invented rather than copied from a prior search
result is rejected here, in code (app/agent/grounding.py), never by asking
the model nicely not to hallucinate.
"""

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.grounding import GroundingRegistry
from app.agent.tools.base import ToolExecutionError, ToolSpec, register_tool
from app.execution.commands import Command, CommandRunner


class RescheduleAppointmentArgs(BaseModel):
    appointment_code: str = Field(
        ..., description="An appointment code previously returned by search_appointments"
    )
    scanner_code: str = Field(
        ...,
        description="A scanner code previously returned by search_appointments or "
        "execute_command, of the correct modality and currently AVAILABLE",
    )
    new_start: str | None = Field(
        None,
        description="ISO 8601 datetime for the new start time; omit to keep the "
        "appointment's current scheduled time and only change the scanner",
    )


async def handle_reschedule_appointment(
    session: AsyncSession, session_id: str, args: RescheduleAppointmentArgs
) -> dict:
    grounding = GroundingRegistry(session)
    await grounding.require_grounded(session_id, "appointment", args.appointment_code)
    await grounding.require_grounded(session_id, "scanner", args.scanner_code)

    command_args: dict[str, str] = {
        "appointment_code": args.appointment_code,
        "scanner_code": args.scanner_code,
    }
    if args.new_start:
        command_args["new_start"] = args.new_start

    result = await CommandRunner(session).execute(Command("reassign_scanner", command_args))
    if not result.success:
        raise ToolExecutionError(
            result.error or "reschedule_appointment failed", result.error_category or "tool_error"
        )

    # The appointment still exists under the same code post-update, and the
    # scanner it now points to is still a valid reference — keep both
    # grounded so a follow-up tool call in the same round can act on them.
    await grounding.expose(session_id, "appointment", [result.data["code"]])
    await grounding.expose(session_id, "scanner", [result.data["scanner"]["code"]])
    return result.data


RESCHEDULE_APPOINTMENT_TOOL = register_tool(
    ToolSpec(
        name="reschedule_appointment",
        description=(
            "Move an appointment to a different scanner (and optionally a new start "
            "time). Both appointment_code and scanner_code MUST come from a prior "
            "search_appointments (or execute_command) result in this conversation — "
            "inventing a code will be rejected. The target scanner must match the "
            "appointment's modality and currently be AVAILABLE."
        ),
        args_schema=RescheduleAppointmentArgs,
        handler=handle_reschedule_appointment,
        is_write=True,
    )
)

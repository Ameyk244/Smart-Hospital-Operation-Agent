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
from app.config import get_settings
from app.db.models.agent import EventStatus
from app.db.repositories.event_repository import BED_ACTION_PROPOSED, RESCHEDULE_PROPOSED
from app.execution.commands import Command, CommandRunner
from app.observability.tracing import record_event


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

    if get_settings().require_reschedule_confirmation:
        proposal = await record_event(
            session,
            session_id=session_id,
            round_num=0,
            event_type=RESCHEDULE_PROPOSED,
            status=EventStatus.SUCCESS,
            tool_name="reschedule_appointment",
            arguments=command_args,
        )
        return {
            "status": "pending_confirmation",
            "proposal_id": proposal.id,
            "message": (
                "The appointment has NOT been changed. The user must press Confirm "
                "in the app to apply this move; do not tell them it was moved."
            ),
        }

    # The actor is recorded on the mutation's audit record (Phase 6).
    result = await CommandRunner(session, actor=f"agent_session:{session_id}").execute(
        Command("reassign_scanner", command_args)
    )
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


class AssignBedArgs(BaseModel):
    patient_code: str = Field(
        ..., description="A patient code previously returned by search_queue"
    )
    bed_code: str = Field(
        ..., description="A bed code previously returned by search_beds, currently AVAILABLE"
    )


async def _propose_bed_action(
    session: AsyncSession, session_id: str, tool_name: str, command_args: dict[str, str]
) -> dict:
    proposal = await record_event(
        session,
        session_id=session_id,
        round_num=0,
        event_type=BED_ACTION_PROPOSED,
        status=EventStatus.SUCCESS,
        tool_name=tool_name,
        arguments=command_args,
    )
    return {
        "status": "pending_confirmation",
        "proposal_id": proposal.id,
        "message": (
            "The bed has NOT been changed. The user must press Confirm in the app "
            "to apply this; do not tell them it was done."
        ),
    }


async def handle_assign_bed(session: AsyncSession, session_id: str, args: AssignBedArgs) -> dict:
    grounding = GroundingRegistry(session)
    await grounding.require_grounded(session_id, "patient", args.patient_code)
    await grounding.require_grounded(session_id, "bed", args.bed_code)

    command_args = {"patient_code": args.patient_code, "bed_code": args.bed_code}
    if get_settings().require_bed_confirmation:
        return await _propose_bed_action(session, session_id, "assign_bed", command_args)

    result = await CommandRunner(session, actor=f"agent_session:{session_id}").execute(
        Command("assign_bed", command_args)
    )
    if not result.success:
        raise ToolExecutionError(result.error or "assign_bed failed", result.error_category or "tool_error")
    return result.data


ASSIGN_BED_TOOL = register_tool(
    ToolSpec(
        name="assign_bed",
        description=(
            "Admit a waiting emergency patient to an available bed. patient_code and "
            "bed_code MUST come from a prior search_queue / search_beds result in this "
            "conversation — inventing a code will be rejected. The patient must have a "
            "WAITING queue entry and no bed; the bed must be AVAILABLE."
        ),
        args_schema=AssignBedArgs,
        handler=handle_assign_bed,
        is_write=True,
    )
)


class ReleaseBedArgs(BaseModel):
    bed_code: str = Field(..., description="A bed code previously returned by search_beds")


async def handle_release_bed(session: AsyncSession, session_id: str, args: ReleaseBedArgs) -> dict:
    await GroundingRegistry(session).require_grounded(session_id, "bed", args.bed_code)

    command_args = {"bed_code": args.bed_code}
    if get_settings().require_bed_confirmation:
        return await _propose_bed_action(session, session_id, "release_bed", command_args)

    result = await CommandRunner(session, actor=f"agent_session:{session_id}").execute(
        Command("release_bed", command_args)
    )
    if not result.success:
        raise ToolExecutionError(result.error or "release_bed failed", result.error_category or "tool_error")
    return result.data


RELEASE_BED_TOOL = register_tool(
    ToolSpec(
        name="release_bed",
        description=(
            "Discharge the patient in an occupied bed and mark the bed for cleaning. "
            "bed_code MUST come from a prior search_beds result in this conversation — "
            "inventing a code will be rejected. The bed must be OCCUPIED."
        ),
        args_schema=ReleaseBedArgs,
        handler=handle_release_bed,
        is_write=True,
    )
)

"""Search/retrieval tools (concept 3 of the tool inventory, docs/
ARCHITECTURE.md §4).

Why it exists: `search_appointments` is the agent's window into hospital
data and the first place grounding actually happens — every appointment,
scanner, and patient code a search returns becomes something a later write
tool is *allowed* to reference (see app/agent/grounding.py). It routes
through the exact same `Command("search_appointments", ...)` the
deterministic parser could use, not a second implementation.
"""

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.grounding import GroundingRegistry
from app.agent.tools.base import ToolExecutionError, ToolSpec, register_tool
from app.execution.commands import Command, CommandRunner


class SearchAppointmentsArgs(BaseModel):
    status: str | None = Field(
        None, description="One of SCHEDULED, DELAYED, COMPLETED, CANCELLED"
    )
    appointment_type: str | None = Field(None, description="One of MRI, CT, XRAY")
    patient_code: str | None = Field(None, description="A patient code, e.g. PT-1001")
    department_code: str | None = Field(None, description="A department code, e.g. DEPT-RAD")
    scanner_code: str | None = Field(None, description="A scanner code, e.g. SCN-1")
    limit: int = Field(25, ge=1, le=100)


async def handle_search_appointments(
    session: AsyncSession, session_id: str, args: SearchAppointmentsArgs
) -> list[dict]:
    command_args = args.model_dump(exclude_none=True)
    result = await CommandRunner(session).execute(Command("search_appointments", command_args))
    if not result.success:
        raise ToolExecutionError(result.error or "search_appointments failed", result.error_category or "tool_error")

    appointments: list[dict] = result.data
    grounding = GroundingRegistry(session)
    await grounding.expose(session_id, "appointment", [a["code"] for a in appointments])
    await grounding.expose(
        session_id, "scanner", [a["scanner"]["code"] for a in appointments if a["scanner"]]
    )
    await grounding.expose(session_id, "patient", [a["patient"]["code"] for a in appointments])
    return appointments


SEARCH_APPOINTMENTS_TOOL = register_tool(
    ToolSpec(
        name="search_appointments",
        description=(
            "Search hospital appointments by status, modality, patient, department, or "
            "scanner. Returns a list of appointments, each with its code, patient, "
            "department, assigned scanner (if any), and status. Only codes returned by "
            "this tool (or other search/read tools) may later be passed to write tools "
            "like reschedule_appointment."
        ),
        args_schema=SearchAppointmentsArgs,
        handler=handle_search_appointments,
        is_write=False,
    )
)


class SearchScannersArgs(BaseModel):
    type: str | None = Field(None, description="One of MRI, CT, XRAY")
    status: str | None = Field(None, description="One of AVAILABLE, IN_USE, MAINTENANCE")
    department_code: str | None = Field(
        None,
        description="A department code, e.g. DEPT-RAD, to list only the scanners that "
        "sit in that department's rooms",
    )


async def handle_search_scanners(
    session: AsyncSession, session_id: str, args: SearchScannersArgs
) -> list[dict]:
    command_args = args.model_dump(exclude_none=True)
    result = await CommandRunner(session).execute(Command("list_scanners", command_args))
    if not result.success:
        raise ToolExecutionError(result.error or "search_scanners failed", result.error_category or "tool_error")

    scanners: list[dict] = result.data
    await GroundingRegistry(session).expose(session_id, "scanner", [s["code"] for s in scanners])
    return scanners


SEARCH_SCANNERS_TOOL = register_tool(
    ToolSpec(
        name="search_scanners",
        description=(
            "List scanners, optionally narrowed by modality, status, or department. "
            "Use this (not execute_command) whenever the scanners are limited by "
            "department, e.g. 'the scanners used by the Radiology department'. Returns "
            "each scanner's code, type and status, and makes those scanner codes usable "
            "in later lookups and reschedules."
        ),
        args_schema=SearchScannersArgs,
        handler=handle_search_scanners,
        is_write=False,
    )
)

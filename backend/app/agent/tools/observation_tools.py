"""Observation/read tools (docs/ARCHITECTURE.md §4, category 6).

Why it exists: `get_scanner_availability` is a small, deliberately narrow
tool — one scanner's live status by code — that exists mainly to prove a
point the broader `search_appointments`/`execute_command` tools don't:
grounding-by-rejection applies to *reads* of a specific entity too, not only
writes. Looking up detail on a scanner the model invented is exactly as
wrong as rescheduling onto one, even though nothing gets mutated either way.
"""

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.grounding import GroundingRegistry
from app.agent.tools.base import ToolExecutionError, ToolSpec, register_tool
from app.execution.commands import Command, CommandRunner


class GetScannerAvailabilityArgs(BaseModel):
    scanner_code: str = Field(
        ...,
        description="A scanner code previously returned by search_appointments or "
        "execute_command in this conversation",
    )


async def handle_get_scanner_availability(
    session: AsyncSession, session_id: str, args: GetScannerAvailabilityArgs
) -> dict:
    grounding = GroundingRegistry(session)
    await grounding.require_grounded(session_id, "scanner", args.scanner_code)

    result = await CommandRunner(session).execute(
        Command("get_scanner_availability", {"scanner_code": args.scanner_code})
    )
    if not result.success:
        raise ToolExecutionError(
            result.error or "get_scanner_availability failed", result.error_category or "tool_error"
        )
    return result.data


GET_SCANNER_AVAILABILITY_TOOL = register_tool(
    ToolSpec(
        name="get_scanner_availability",
        description=(
            "Get the current live status (AVAILABLE/IN_USE/MAINTENANCE) of one "
            "specific scanner by code. scanner_code must come from a prior search or "
            "execute_command result in this conversation — an invented code will be "
            "rejected."
        ),
        args_schema=GetScannerAvailabilityArgs,
        handler=handle_get_scanner_availability,
        is_write=False,
    )
)

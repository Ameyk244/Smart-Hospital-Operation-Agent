"""The deterministic command endpoint.

Why it exists: the API-reachable half of docs/ARCHITECTURE.md §1's left-hand
path. Tries the parser first, on every request; a match executes through
`CommandRunner` and returns. Not matched simply reports that — this route
does not itself decide to invoke the agent. That decision belongs to
`app/agent/eligibility.py` (a later phase); wiring it in here would blur the
"deterministic vs. agent" boundary the whole project is built to keep
visible.

What calls it: the frontend's chat/command input (once it exists);
`tests/e2e/*`.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.execution.commands import Command, CommandRunner
from app.parser.parser import parse

router = APIRouter(prefix="/api/commands", tags=["commands"])


class CommandRequest(BaseModel):
    text: str


class CommandResponse(BaseModel):
    matched: bool
    command_name: str | None = None
    success: bool | None = None
    data: object | None = None
    error: str | None = None
    error_category: str | None = None


@router.post("", response_model=CommandResponse)
async def run_command(
    request: CommandRequest, db: AsyncSession = Depends(get_db)
) -> CommandResponse:
    outcome = parse(request.text)
    if not outcome.matched:
        return CommandResponse(matched=False)

    command: Command = outcome.command  # type: ignore[assignment]
    result = await CommandRunner(db).execute(command)
    return CommandResponse(
        matched=True,
        command_name=command.name,
        success=result.success,
        data=result.data,
        error=result.error,
        error_category=result.error_category,
    )

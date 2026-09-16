"""The command-decomposition tool (concepts 19, 20, 21).

Why it exists: `execute_command` lets the LLM identify a natural-language
sub-instruction ("list delayed CT appointments") and route it through the
*exact same* deterministic parser + `CommandRunner` the no-LLM path uses
(docs/ARCHITECTURE.md §2) — the agent doesn't get its own private grammar or
its own private execution logic, it reuses the one that already exists.

Architectural property worth being explicit about: the deterministic
grammar in `app/parser/parser.py` only ever builds *read* commands — there
is no grammar rule anywhere that produces `Command("reassign_scanner", ...)`.
That's deliberate, not an oversight: it means `execute_command` cannot be
used as a backdoor to mutate hospital data no matter how the LLM phrases a
request, because the shared grammar it decomposes into structurally has
nothing to mutate with. The only path to that one write operation is the
dedicated, grounded `reschedule_appointment` tool
(`app/agent/tools/action_tools.py`). `tests/integration/test_command_tools.py`
has an adversarial test proving this holds even when a scripted model tries.

What calls it: the graph exactly like any other tool — see
`app/agent/tools/base.py` for the registration mechanism.
"""

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.grounding import GroundingRegistry
from app.agent.tools.base import ToolExecutionError, ToolSpec, register_tool
from app.execution.commands import CommandRunner
from app.parser.parser import parse

_KNOWN_COMMAND_EXAMPLES = (
    "list departments | list scanners [mri|ct|xray] [available|in use|maintenance] | "
    "show patient <name> | show next appointment | list delayed appointments [mri|ct|xray]"
)


class ExecuteCommandArgs(BaseModel):
    command_text: str = Field(
        ...,
        description=(
            "A short imperative instruction phrased to match this system's fixed "
            f"command grammar. Known shapes: {_KNOWN_COMMAND_EXAMPLES}. This can only "
            "run read/lookup operations — it has no way to mutate data, regardless of "
            "phrasing; use reschedule_appointment for that."
        ),
    )


def _entity_type_of(item: object) -> str | None:
    """Distinguishes AppointmentOut/PatientOut/ScannerOut dicts by shape —
    checked in this order specifically, since an appointment dict also
    contains a nested scanner dict (which would otherwise match the scanner
    check) and DepartmentOut/StaffOut match neither and correctly return
    None."""
    if not isinstance(item, dict):
        return None
    if "appointment_type" in item and "patient" in item:
        return "appointment"
    if "mrn" in item:
        return "patient"
    if "type" in item and "status" in item:
        return "scanner"
    return None


async def _expose_grounding_for_result(
    grounding: GroundingRegistry, session_id: str, data: object
) -> None:
    """Search commands reached via execute_command must ground their
    results exactly like search_appointments does directly — otherwise a
    scanner or appointment code discovered through this path would be
    unusable by a later write tool, defeating the point of decomposition."""
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    grounded: dict[str, list[str]] = {"appointment": [], "scanner": [], "patient": []}
    for item in items:
        entity_type = _entity_type_of(item)
        if entity_type is None:
            continue
        grounded[entity_type].append(item["code"])  # type: ignore[index]
        if entity_type == "appointment":
            if item.get("scanner"):  # type: ignore[union-attr]
                grounded["scanner"].append(item["scanner"]["code"])  # type: ignore[index]
            if item.get("patient"):  # type: ignore[union-attr]
                grounded["patient"].append(item["patient"]["code"])  # type: ignore[index]
    for entity_type, codes in grounded.items():
        if codes:
            await grounding.expose(session_id, entity_type, codes)


async def handle_execute_command(
    session: AsyncSession, session_id: str, args: ExecuteCommandArgs
) -> object:
    outcome = parse(args.command_text)
    if not outcome.matched:
        raise ToolExecutionError(
            f"{args.command_text!r} doesn't match any known command. Known shapes: "
            f"{_KNOWN_COMMAND_EXAMPLES}",
            category="unparseable_command",
        )

    assert outcome.command is not None
    result = await CommandRunner(session).execute(outcome.command)
    if not result.success:
        raise ToolExecutionError(result.error or "command failed", result.error_category or "tool_error")

    await _expose_grounding_for_result(GroundingRegistry(session), session_id, result.data)
    return result.data


EXECUTE_COMMAND_TOOL = register_tool(
    ToolSpec(
        name="execute_command",
        description=(
            "Run a request against this hospital system's fixed set of known "
            f"read/lookup commands: {_KNOWN_COMMAND_EXAMPLES}. Phrase command_text to "
            "match one of these shapes exactly (e.g. 'list delayed appointments ct'). "
            "This is a read-only path — it cannot reschedule or otherwise change "
            "anything; use reschedule_appointment for that, with codes grounded from "
            "a search result (including one returned by this tool)."
        ),
        args_schema=ExecuteCommandArgs,
        handler=handle_execute_command,
        is_write=False,
    )
)

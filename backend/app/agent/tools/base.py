"""The tool registry (concepts 6, 7, 24, 25).

Why it exists: one place every agent tool is declared, with its argument
schema, description, a handler function, and whether it's a read or a write
(concept 25) — used by `app/agent/graph.py`'s tool_node to decide whether a
grounding check applies before execution. Deliberately hand-rolled rather
than using LangChain's `@tool` decorator + prebuilt `ToolNode`: this project
needs to inject its own validation → grounding → timeout → observability
pipeline around every call (docs/ARCHITECTURE.md §5, §6, §9), which a
prebuilt `ToolNode` would hide exactly the way the master prompt says not
to.

What calls it: each `app/agent/tools/*_tools.py` module registers its tools
here at import time; `app/agent/graph.py` reads `TOOL_REGISTRY` to bind
tools to the model and to dispatch tool_node calls.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

# A tool handler receives its own fresh DB session (see app/agent/graph.py's
# tool_node — each tool call gets its own session/transaction, independent
# of any request-scoped session), the calling session_id (for grounding and
# preference lookups), and its already-Pydantic-validated args.
ToolHandler = Callable[[AsyncSession, str, BaseModel], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_schema: type[BaseModel]
    handler: ToolHandler
    # Read vs. write (concept 25): informs logging/observability categorization.
    # Grounding itself is enforced inside each handler (app/agent/grounding.py),
    # not generically here — different write tools ground different fields
    # (reschedule_appointment checks both an appointment_code and a
    # scanner_code), so there's no single "the" entity type to hang a
    # generic check off of.
    is_write: bool


class ToolExecutionError(Exception):
    """Raised by a tool handler when the underlying operation failed for a
    normal, expected reason (a `CommandError`/not-found from `CommandRunner`,
    typically). The tool_node turns this into a `ToolMessage` describing the
    failure so the model can see it and react, instead of the graph
    crashing."""

    def __init__(self, message: str, category: str = "tool_error") -> None:
        super().__init__(message)
        self.category = category


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> ToolSpec:
    if spec.name in TOOL_REGISTRY:
        raise ValueError(f"Tool {spec.name!r} is already registered")
    TOOL_REGISTRY[spec.name] = spec
    return spec


def to_openai_tool_dict(spec: ToolSpec) -> dict[str, Any]:
    """Converts a ToolSpec into the OpenAI-style tool-calling schema that
    `BaseChatModel.bind_tools` accepts uniformly across providers —
    LangChain normalizes both Anthropic's and OpenAI's wire formats to/from
    this shape, which is what keeps `app/agent/graph.py` provider-agnostic."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.args_schema.model_json_schema(),
        },
    }


def all_tool_dicts() -> list[dict[str, Any]]:
    return [to_openai_tool_dict(spec) for spec in TOOL_REGISTRY.values()]

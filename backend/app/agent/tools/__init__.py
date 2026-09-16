"""Importing this package registers every agent tool. Anything that needs
`TOOL_REGISTRY` populated — the graph builder, the API startup — must import
`app.agent.tools` (not just `app.agent.tools.base`) at least once first.
"""

from app.agent.tools import (  # noqa: F401
    action_tools,
    command_tools,
    memory_tools,
    observation_tools,
    search_tools,
)
from app.agent.tools.base import TOOL_REGISTRY, ToolExecutionError, ToolSpec, all_tool_dicts

__all__ = ["TOOL_REGISTRY", "ToolExecutionError", "ToolSpec", "all_tool_dicts"]

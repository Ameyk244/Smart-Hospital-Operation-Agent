"""Importing this package registers every command handler (each module
below calls `@register(...)` at import time). Anything that needs
`CommandRunner` to actually know about commands — the parser, the API
startup, the agent tools — must import `app.execution.commands` (not just
`app.execution.commands.base`) at least once first.
"""

from app.execution.commands import (  # noqa: F401
    appointment_commands,
    patient_commands,
    reference_commands,
)
from app.execution.commands.base import Command, CommandError, CommandResult, CommandRunner

__all__ = ["Command", "CommandError", "CommandResult", "CommandRunner"]

"""The trusted execution chokepoint (concepts 18, 19, 21).

Why it exists: this is the *one* place both entrances in
docs/ARCHITECTURE.md §1 converge. The deterministic parser builds a
`Command` from recognized text; the agent's `execute_command` tool builds
one from a natural-language fragment it decomposed; the agent's dedicated
write tool (`reschedule_appointment`) builds one from already-grounded
arguments. All three call the same
`CommandRunner.execute()`, which dispatches to the same handler function per
`command.name` — there is no second implementation of any hospital
operation anywhere else in the codebase.

What calls it: `app/parser/parser.py` (deterministic path),
`app/agent/tools/command_tools.py` and `app/agent/tools/action_tools.py`
(agent path).

Fails: a handler raises `CommandError` for a business-rule violation (e.g.
"scanner is not available") or `ValueError` for an unknown entity code —
`CommandRunner.execute` catches both, rolls back, and returns a
`CommandResult(success=False, ...)` rather than letting an exception
propagate to the caller. Everything else (a real bug) is deliberately left
to propagate; a `CommandResult` should never hide a programming error as a
graceful failure.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class CommandError(Exception):
    """A business-rule failure a caller should treat as a normal, expected
    outcome (not a bug) — e.g. "that scanner isn't available". Carries a
    short `category` so observability logging (concept 49) can bucket
    failures without parsing message strings."""

    def __init__(self, message: str, category: str = "business_rule") -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Command:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandResult:
    success: bool
    data: Any = None
    error: str | None = None
    error_category: str | None = None


CommandHandler = Callable[[AsyncSession, dict[str, Any]], Awaitable[Any]]

_REGISTRY: dict[str, CommandHandler] = {}


def register(name: str) -> Callable[[CommandHandler], CommandHandler]:
    """Decorator each command module uses to add itself to the registry.
    Raises at import time (not at call time) if a name is reused — a
    duplicate command name is always a programming error, never a valid
    runtime state."""

    def decorator(fn: CommandHandler) -> CommandHandler:
        if name in _REGISTRY:
            raise ValueError(f"Command {name!r} is already registered")
        _REGISTRY[name] = fn
        return fn

    return decorator


def known_command_names() -> list[str]:
    return sorted(_REGISTRY)


class CommandRunner:
    """The single execution entrypoint. See module docstring."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def execute(self, command: Command) -> CommandResult:
        handler = _REGISTRY.get(command.name)
        if handler is None:
            return CommandResult(
                success=False,
                error=f"Unknown command {command.name!r}",
                error_category="unknown_command",
            )
        try:
            data = await handler(self._session, command.args)
        except CommandError as exc:
            await self._session.rollback()
            return CommandResult(success=False, error=str(exc), error_category=exc.category)
        except ValueError as exc:
            await self._session.rollback()
            return CommandResult(success=False, error=str(exc), error_category="not_found")
        else:
            await self._session.commit()
            return CommandResult(success=True, data=data)

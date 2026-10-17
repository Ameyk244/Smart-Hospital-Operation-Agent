"""In-memory registry of the `ToolTurn` for each in-flight harness turn.

The bridge registers a turn before calling the harness and removes it in
`finally`; the internal tool-call route looks it up by `turn_id`. A plain dict
is enough: everything runs on one event loop.
"""

from typing import Any

_turns: dict[str, Any] = {}


def register(turn_id: str, turn: Any) -> None:
    _turns[turn_id] = turn


def get(turn_id: str) -> Any | None:
    return _turns.get(turn_id)


def unregister(turn_id: str) -> None:
    _turns.pop(turn_id, None)

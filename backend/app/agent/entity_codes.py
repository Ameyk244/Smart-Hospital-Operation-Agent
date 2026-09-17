"""Pulls every entity `code` out of a command/tool result.

Why it exists: the frontend's Operations panel wants to briefly highlight
whichever rows a chat turn actually touched (Task 4 of the UI work) —
whatever appointment/scanner/patient/department codes appeared anywhere in
that turn's command or tool results. Every hospital entity schema
(`app/schemas/hospital.py`) uses `code` as its business key, including in
nested relations (an `AppointmentOut` embeds a `PatientOut` and a
`ScannerOut`, each with their own `code`), so one small recursive walk
covers every entity type and every command/tool shape without needing a
per-entity-type case, unlike the grounding-exposure logic in
`app/agent/tools/command_tools.py` (which *does* need to know the entity
*type*, for the grounding ledger — a different, stricter concern than "which
rows changed color for a second").

What calls it: `app/api/routes/chat.py` (deterministic path, straight from
`CommandResult.data`) and `app/agent/graph.py`'s tool_node (agent path,
per successful tool call, accumulated across the turn).
"""

from typing import Any


def extract_entity_codes(data: Any) -> list[str]:
    """Order-preserving, de-duplicated list of every `code` field found
    anywhere in `data` (dicts and lists, arbitrarily nested)."""
    codes: list[str] = []
    seen: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            code = node.get("code")
            if isinstance(code, str) and code not in seen:
                seen.add(code)
                codes.append(code)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return codes

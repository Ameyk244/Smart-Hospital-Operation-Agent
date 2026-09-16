"""Extracts human-readable text from a LangChain message's `content`.

Why it exists: `AIMessage.content` is not always a plain string. When a
model returns multiple content blocks in one turn — Claude Sonnet 5 can
include a `thinking` block alongside the actual `text` block even without
extended thinking explicitly requested — LangChain represents `.content` as
a list of block dicts instead of a string. Anything that shows a model's
reply to a human (the chat API's response, a test asserting on a live
reply) needs this function.

Anything that stores or replays messages *for the model itself* — the
`AgentState`/checkpointed messages in `app/agent/graph.py`, which get fed
back into `model_with_tools.ainvoke(...)` on the next round — must NOT use
this and must NOT have this applied to what's stored in state. Anthropic's
protocol requires a prior turn's `thinking` block (with its signature) to
be replayed unmodified on subsequent turns; stripping it there wouldn't fix
a display bug, it would break multi-turn correctness. This function exists
specifically for the human-facing boundary, not the model-facing one.

What calls it: `app/api/routes/chat.py` (the one place a model reply
becomes both the HTTP response and the stored `ConversationMessage` text —
fixing extraction there fixes both at once, since the same string is used
for each). Also used by tests asserting on a live model's final answer,
for the same reason.
"""

from typing import Any


def extract_text_content(content: str | list[Any]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        # Any other block type (thinking, redacted_thinking, tool_use, ...)
        # is intentionally skipped — none of those are human-readable reply
        # text.
    return "".join(parts)

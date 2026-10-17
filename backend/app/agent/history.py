"""Converts stored conversation turns into LangChain messages.

Why it exists: `ConversationMessage` rows (concept 35, `app/db/models/
agent.py`) are the durable, role-tagged record of a session's transcript.
The agent graph needs that same history as LangChain `BaseMessage` objects
to actually use it as context (concept 42) — this is the one, small
conversion between those two representations, so it isn't reinvented at
each call site. `to_openai_messages` is the other direction of the same
idea: LangChain messages to the OpenAI-format dicts the harness contract
(docs/harness-smart-hospital-operations.md section 5) carries as `history`.
"""

import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agent.message_text import extract_text_content
from app.db.models.agent import ConversationMessage, MessageRole


def to_langchain_messages(rows: list[ConversationMessage]) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    for row in rows:
        if row.role == MessageRole.USER:
            messages.append(HumanMessage(content=row.content))
        elif row.role == MessageRole.ASSISTANT:
            messages.append(AIMessage(content=row.content))
        # TOOL/SYSTEM rows are not currently written to ConversationMessage
        # (tool exchanges live only in the in-flight graph state, per
        # docs/ARCHITECTURE.md §7's separation of concerns) — skip rather
        # than guess a mapping if one ever appears.
    return messages


def to_openai_messages(messages: list[BaseMessage]) -> list[dict]:
    """Prior turns as OpenAI-format messages. System messages are dropped:
    the harness gets a fresh system prompt every turn."""
    converted: list[dict] = []
    for message in messages:
        content = extract_text_content(message.content)
        if isinstance(message, HumanMessage):
            converted.append({"role": "user", "content": content})
        elif isinstance(message, AIMessage):
            entry: dict = {"role": "assistant", "content": content}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
                    }
                    for call in message.tool_calls
                ]
            converted.append(entry)
        elif isinstance(message, ToolMessage):
            converted.append(
                {"role": "tool", "content": content, "tool_call_id": message.tool_call_id}
            )
    return converted

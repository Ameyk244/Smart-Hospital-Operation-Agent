"""Converts stored conversation turns into LangChain messages.

Why it exists: `ConversationMessage` rows (concept 35, `app/db/models/
agent.py`) are the durable, role-tagged record of a session's transcript.
The agent graph needs that same history as LangChain `BaseMessage` objects
to actually use it as context (concept 42) — this is the one, small
conversion between those two representations, so it isn't reinvented at
each call site.
"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

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

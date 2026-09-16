"""Typed LangGraph state (concept 13).

Why it exists: a plain dict state makes it impossible to know what a node
reads/writes without reading every node. `AgentState` is the one place that
contract lives. `messages` uses LangGraph's `add_messages` reducer so nodes
append to the transcript by returning only the new message(s), not the
whole list — the standard, non-magic way LangGraph merges state across node
returns.
"""

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    session_id: str
    messages: Annotated[list[BaseMessage], add_messages]
    round_count: int
    tool_call_count: int
    invalid_call_count: int
    terminated_reason: str | None

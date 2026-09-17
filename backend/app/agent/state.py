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
    # Deliberately NOT a reducer-merged field (unlike `messages`): like
    # round_count/tool_call_count, this must reset to [] on every fresh
    # run_agent() call regardless of what a resumed checkpoint thread
    # carries, since it means "entity codes this turn touched" — not
    # "ever". tool_node accumulates into it manually across the rounds
    # *within* one turn; run_agent's initial_state explicitly overwrites
    # it to [] at the start of every turn, the same way it resets the
    # other counters. See app/agent/entity_codes.py for what populates it.
    touched_entity_codes: list[str]

"""ToolTurn tests: one turn's tool calls driven directly, with no model and no
graph. Covers the counters and termination reasons the agent loop relies on.
"""

import pytest
from sqlalchemy import select

from app.agent.turn import ToolTurn
from app.db.models.agent import AgentEvent, EventStatus
from app.db.repositories.session_repository import SessionRepository
from tests.integration.test_agent_loop import _settings

pytestmark = pytest.mark.integration

SESSION_ID = "test-tool-turn"
FABRICATED_RESCHEDULE = {"appointment_code": "APT-9999", "scanner_code": "SCN-1"}


async def _turn(session_factory, **settings) -> ToolTurn:
    async with session_factory() as session:
        await SessionRepository(session).get_or_create(SESSION_ID)
        await session.commit()
    return ToolTurn(session_id=SESSION_ID, session_factory=session_factory, settings=_settings(**settings))


async def _call(turn: ToolTurn, name: str, args: dict | None) -> str:
    return await turn.call(name=name, args=args, round_num=1)


async def test_unparseable_arguments_count_as_invalid(agent_session_factory):
    turn = await _turn(agent_session_factory)

    content = await _call(turn, "search_appointments", None)

    assert content == "Invalid arguments: could not parse JSON"
    assert turn.invalid_call_count == 1
    assert turn.tool_call_count == 0


async def test_unknown_tool_is_rejected_and_recorded(agent_session_factory):
    turn = await _turn(agent_session_factory)

    content = await _call(turn, "delete_everything", {})

    assert content == "Unknown tool 'delete_everything'"
    assert turn.invalid_call_count == 1
    async with agent_session_factory() as session:
        events = (
            await session.scalars(select(AgentEvent).where(AgentEvent.tool_name == "delete_everything"))
        ).all()
    assert {(e.event_type, e.error_category) for e in events} == {("tool_requested", "unknown_tool")}


async def test_grounding_rejection_counts_as_a_call_and_as_invalid(agent_session_factory):
    turn = await _turn(agent_session_factory)

    content = await _call(turn, "reschedule_appointment", FABRICATED_RESCHEDULE)

    assert "never exposed" in content
    assert (turn.tool_call_count, turn.invalid_call_count) == (1, 1)
    assert turn.terminated_reason is None


async def test_tool_call_cap_ends_the_turn_and_skips_later_calls(agent_session_factory):
    turn = await _turn(agent_session_factory, max_tool_calls=1)

    await _call(turn, "search_scanners", {})
    skipped = await _call(turn, "search_scanners", {})

    assert skipped == "Not executed: this session's tool-call budget is exhausted."
    assert turn.terminated_reason == "max_tool_calls_exceeded"
    assert turn.tool_call_count == 1


async def test_invalid_call_cap_ends_the_turn(agent_session_factory):
    turn = await _turn(agent_session_factory, max_invalid_tool_calls=2)

    await _call(turn, "delete_everything", {})
    assert turn.terminated_reason is None
    await _call(turn, "delete_everything", {})

    assert turn.terminated_reason == "too_many_invalid_calls"


WRITE_LIMIT_MESSAGE = "Not executed: this turn's write-call limit was reached."


def _remember(key: str) -> dict:
    return {"key": key, "value": "x"}


async def test_write_cap_is_off_by_default(agent_session_factory):
    turn = await _turn(agent_session_factory)

    contents = [await _call(turn, "remember_preference", _remember(f"k{i}")) for i in range(3)]

    assert WRITE_LIMIT_MESSAGE not in contents
    assert turn.write_call_count == 3


async def test_write_cap_rejects_the_next_write_but_not_reads(agent_session_factory):
    turn = await _turn(agent_session_factory, max_write_calls_per_turn=1)

    first = await _call(turn, "remember_preference", _remember("a"))
    second = await _call(turn, "remember_preference", _remember("b"))
    read = await _call(turn, "list_preferences", {})

    assert '"remembered": true' in first
    assert second == WRITE_LIMIT_MESSAGE
    assert '"key": "a"' in read and '"key": "b"' not in read
    assert (turn.write_call_count, turn.tool_call_count, turn.invalid_call_count) == (1, 2, 1)
    assert turn.terminated_reason is None
    async with agent_session_factory() as session:
        events = (
            await session.scalars(
                select(AgentEvent).where(
                    AgentEvent.session_id == SESSION_ID,
                    AgentEvent.error_category == "write_limit_exceeded",
                )
            )
        ).all()
    assert [(e.event_type, e.status, e.tool_name) for e in events] == [
        ("tool_requested", EventStatus.REJECTED, "remember_preference")
    ]


async def test_invalid_write_call_is_not_counted_as_a_write_attempt(agent_session_factory):
    turn = await _turn(agent_session_factory, max_write_calls_per_turn=1)

    await _call(turn, "remember_preference", {"key": "a"})
    content = await _call(turn, "remember_preference", _remember("a"))

    assert content != WRITE_LIMIT_MESSAGE
    assert (turn.write_call_count, turn.invalid_call_count) == (1, 1)


async def test_failed_write_does_not_use_the_cap(agent_session_factory):
    turn = await _turn(agent_session_factory, max_write_calls_per_turn=1)

    rejected = await _call(turn, "assign_bed", {"patient_code": "PT-1", "bed_code": "BED-1"})
    retry = await _call(turn, "remember_preference", _remember("a"))

    assert rejected != WRITE_LIMIT_MESSAGE and retry != WRITE_LIMIT_MESSAGE
    assert turn.write_call_count == 1

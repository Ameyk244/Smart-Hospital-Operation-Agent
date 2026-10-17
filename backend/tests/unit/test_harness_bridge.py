"""Pure parts of the harness bridge: OpenAI history conversion, the message
rebuild and the reason-to-reply mapping. No HTTP, no database."""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.harness_bridge import (
    NOT_EXECUTED,
    HarnessCall,
    HarnessFinish,
    HarnessRound,
    _RecordingTurn,
    rebuild_turn_messages,
    resolve_outcome,
)
from app.agent.history import to_openai_messages


def test_history_conversion_roles_and_json_arguments():
    messages = [
        SystemMessage(content="prompt"),
        HumanMessage(content="find scanners"),
        AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "search_scanners", "args": {"scanner_code": "SCN-1"}}],
        ),
        ToolMessage(content="[]", tool_call_id="c1"),
        AIMessage(content="none found"),
    ]
    assert to_openai_messages(messages) == [
        {"role": "user", "content": "find scanners"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "search_scanners", "arguments": '{"scanner_code": "SCN-1"}'},
                }
            ],
        },
        {"role": "tool", "content": "[]", "tool_call_id": "c1"},
        {"role": "assistant", "content": "none found"},
    ]


def test_history_conversion_extracts_text_from_block_content():
    blocks = [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "answer"}]
    assert to_openai_messages([AIMessage(content=blocks)]) == [
        {"role": "assistant", "content": "answer"}
    ]


def _round(n, text="", calls=()):
    return HarnessRound(
        round=n, text=text, tool_calls=[HarnessCall(id=i, name=name, arguments=a) for i, name, a in calls]
    )


def test_rebuild_matches_reused_call_ids_per_round():
    rounds = [
        _round(1, calls=[("call_0", "search_scanners", "{}")]),
        _round(2, calls=[("call_0", "search_appointments", '{"a": 1}')]),
        _round(3, text="done"),
    ]
    served = [(1, "call_0", "search_scanners", "first"), (2, "call_0", "search_appointments", "second")]
    messages = rebuild_turn_messages(rounds, served, "done")
    tool_contents = [m.content for m in messages if isinstance(m, ToolMessage)]
    assert tool_contents == ["first", "second"]
    assert messages[2].tool_calls[0]["args"] == {"a": 1}


def test_rebuild_closes_unserved_calls_and_tolerates_bad_arguments():
    rounds = [_round(1, calls=[("c1", "search_scanners", "not json")])]
    messages = rebuild_turn_messages(rounds, [], "stopped")
    assert messages[0].tool_calls[0]["args"] == {}
    assert messages[1] == ToolMessage(content=NOT_EXECUTED, tool_call_id="c1")
    assert messages[-1] == AIMessage(content="stopped")


def test_rebuild_keeps_served_calls_the_rounds_never_mention():
    messages = rebuild_turn_messages([], [(1, "c9", "search_scanners", "result")], "reply")
    assert messages[0].tool_calls == [{"id": "c9", "name": "search_scanners", "args": {}, "type": "tool_call"}]
    assert messages[1] == ToolMessage(content="result", tool_call_id="c9")
    assert messages[-1] == AIMessage(content="reply")


def test_rebuild_does_not_duplicate_a_clean_completion():
    messages = rebuild_turn_messages([_round(1, text="all done")], [], "all done")
    assert messages == [AIMessage(content="all done")]


def test_rebuild_always_ends_with_an_assistant_message_without_tool_calls():
    rounds = [_round(1, calls=[("c1", "search_scanners", "{}")])]
    messages = rebuild_turn_messages(rounds, [(1, "c1", "search_scanners", "ok")], "limit reached")
    assert isinstance(messages[-1], AIMessage)
    assert not messages[-1].tool_calls


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("round_limit", "max_rounds_exceeded"), ("time_limit", "llm_timeout"), ("error", "harness_error")],
)
def test_stop_reasons_map_to_terminated_reasons_with_fixed_replies(stop_reason, expected):
    finish = HarnessFinish(stop_reason=stop_reason, error="provider said secret", model_text="partial")
    reason, reply = resolve_outcome(None, finish)
    assert reason == expected
    assert "secret" not in reply
    assert "partial" not in reply


def test_clean_completion_uses_model_text():
    assert resolve_outcome(None, HarnessFinish(model_text="hello")) == (None, "hello")


def test_backend_end_turn_wins_over_harness_stop_reason():
    finish = HarnessFinish(stop_reason="time_limit")
    reason, _ = resolve_outcome("too_many_invalid_calls", finish)
    assert reason == "too_many_invalid_calls"


def test_unreachable_harness_is_unavailable():
    reason, reply = resolve_outcome(None, None)
    assert reason == "harness_unavailable"
    assert reply


def test_rebuild_puts_unreported_calls_before_the_final_answer():
    rounds = [_round(2, text="all done")]
    messages = rebuild_turn_messages(rounds, [(1, "c9", "search_scanners", "result")], "all done")
    assert [type(m) for m in messages] == [AIMessage, ToolMessage, AIMessage]
    assert messages[-1] == AIMessage(content="all done")


def test_a_clean_finish_with_no_text_is_a_harness_error():
    reason, reply = resolve_outcome(None, HarnessFinish(model_text="  "))
    assert reason == "harness_error"
    assert reply


class _SlowTurn:
    terminated_reason = None

    def __init__(self):
        self.running = 0
        self.overlap = False
        self.runs = 0

    async def call(self, *, name, args, round_num, parent_context=None):
        self.running += 1
        self.overlap |= self.running > 1
        self.runs += 1
        await asyncio.sleep(0.01)
        self.running -= 1
        return f"result {self.runs}"


async def test_recorded_calls_run_one_at_a_time_and_a_replay_is_not_rerun():
    turn = _SlowTurn()
    recorder = _RecordingTurn(turn, context=None)
    first, other, replay = await asyncio.gather(
        recorder.call(call_id="a", name="t", args={}, round_num=1),
        recorder.call(call_id="b", name="t", args={}, round_num=1),
        recorder.call(call_id="a", name="t", args={}, round_num=1),
    )
    assert not turn.overlap
    assert turn.runs == 2
    assert replay == first
    assert other != first


async def test_drain_waits_for_a_call_already_running():
    turn = _SlowTurn()
    recorder = _RecordingTurn(turn, context=None)
    call = asyncio.create_task(recorder.call(call_id="a", name="t", args={}, round_num=1))
    await asyncio.sleep(0)
    await recorder.drain()
    assert call.done()
    assert recorder.served == [(1, "a", "t", "result 1")]

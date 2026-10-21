"""The whole backend chain in `AGENT_RUNTIME=harness` mode, with no model.

Why it exists: contract section 8.4 of docs/harness-smart-hospital-operations.md.
Every piece is real -- the FastAPI app, `/api/chat`, the test database, the
Postgres checkpointer, the internal tool-call route, `ToolTurn` and the
bridge -- except the one HTTP call to the harness process. That call
(`harness_bridge._post_turn`) is replaced by `FakeHarness`, which plays a
scripted turn: it POSTs each scripted tool call to the real internal route
through the same ASGI client, then returns a finish payload shaped like
contract section 5 (validated with the strict contract models).

No live LLM, no network, no real harness.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select, text

from app.agent import harness_registry
from app.agent.entity_codes import extract_entity_codes
from app.agent.graph import get_thread_messages
from app.api.routes.chat import get_checkpointer
from app.config import Settings
from app.db.models.agent import AgentEvent, EventStatus
from app.db.models.hospital import Appointment
from app.db.session import get_db, get_session_factory
from app.main import app
from tests.unit.test_harness_contract import TurnResponse

pytestmark = pytest.mark.integration

TOKEN = "t" * 32
ASK = "Which MRI appointments are delayed right now?"
FOLLOW_UP = "Which of those appointments is the earliest?"
SEARCH = ("search_appointments", {"appointment_type": "MRI"})
UNGROUNDED_RESCHEDULE = (
    "reschedule_appointment",
    {"appointment_code": "APT-2001", "scanner_code": "SCN-2"},
)
UNKNOWN = ("delete_everything", {})
LOCAL = ("current_time", {})
LOCAL_RESULTS = {"current_time": '{"now":"2026-10-18T14:03:00+05:30","weekday":"Sunday"}'}

UNAVAILABLE = "The assistant is temporarily unavailable. Please try again shortly."
GENERIC_ERROR = "Something went wrong while processing that request. Please try again."


def round_usage(number: int) -> dict:
    return {"prompt_tokens": 100 * number, "completion_tokens": 10 * number, "cached_tokens": 0}


class FakeHarness:
    """Plays one scripted turn: `script` is a list of (tool name, arguments)."""

    def __init__(self, http: AsyncClient) -> None:
        self.http = http
        self.script: list[tuple[str, dict | str]] = []
        self.text = "Here is what I found."
        self.stop_reason: str | None = None
        self.error: str | None = None
        self.raises: Exception | None = None
        self.raw_finish: dict | None = None
        self.mid_turn = None
        self.attempts: list[dict] = []
        self.payloads: list[dict] = []
        self.responses: list[dict] = []
        self.turn_id: str | None = None
        self.turn = None

    async def call_tool(self, turn_id, round_num, name, arguments, token=TOKEN):
        return await self.http.post(
            f"/internal/v1/turns/{turn_id}/tool_calls",
            json={
                "round": round_num,
                "call_id": f"call_{round_num}",
                "name": name,
                "arguments": arguments,
            },
            headers={"x-harness-token": token},
        )

    async def __call__(self, settings, payload):
        self.payloads.append(payload)
        self.turn_id = payload["turn_id"]
        self.turn = harness_registry.get(self.turn_id)
        attempt = self.attempts.pop(0) if self.attempts else {}
        stop_reason = attempt.get("stop_reason", self.stop_reason)
        error = attempt.get("error", self.error)
        reply = attempt.get("text", self.text)
        if self.raises:
            raise self.raises
        rounds = []
        for number, (name, args) in enumerate(self.script, 1):
            raw = args if isinstance(args, str) else json.dumps(args)
            call = {"id": f"call_{number}", "name": name, "arguments": raw}
            if name in LOCAL_RESULTS:
                call["result"] = LOCAL_RESULTS[name]
                rounds.append(
                    {"round": number, "text": "", "tool_calls": [call], "usage": round_usage(number)}
                )
                continue
            body = (await self.call_tool(self.turn_id, number, name, raw)).json()
            self.responses.append(body)
            rounds.append(
                {"round": number, "text": "", "tool_calls": [call], "usage": round_usage(number)}
            )
            if body["end_turn"]:
                break
        if self.mid_turn:
            await self.mid_turn(self)
        if self.raw_finish is not None:
            return self.raw_finish
        number = len(rounds) + 1
        rounds.append(
            {"round": number, "text": reply, "tool_calls": [], "usage": round_usage(number)}
        )
        finish = {
            "turn_id": self.turn_id,
            "stop_reason": stop_reason,
            "error": error,
            "model_text": reply,
            "rounds": len(rounds),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0},
            "assistant_rounds": rounds,
        }
        TurnResponse.model_validate(finish)
        return finish


class World:
    def __init__(self, http, harness, factory, checkpointer, monkeypatch) -> None:
        self.http = http
        self.harness = harness
        self.factory = factory
        self.checkpointer = checkpointer
        self._monkeypatch = monkeypatch
        self.settings = self.configure()

    def configure(self, **overrides) -> Settings:
        values = dict(
            agent_runtime="harness",
            harness_token=TOKEN,
            enable_jev_fast_path=False,
            tool_timeout_seconds=5,
        )
        settings = Settings(**{**values, **overrides})
        for module in ("chat", "internal_harness"):
            self._monkeypatch.setattr(f"app.api.routes.{module}.get_settings", lambda: settings)
        self.settings = settings
        return settings

    async def chat(self, message: str, session_id: str | None = None) -> dict:
        response = await self.http.post("/api/chat", json={"text": message, "session_id": session_id})
        assert response.status_code == 200
        return response.json()

    async def events(self, session_id: str) -> list[tuple]:
        async with self.factory() as session:
            rows = await session.execute(
                select(
                    AgentEvent.event_type, AgentEvent.tool_name, AgentEvent.status, AgentEvent.error_category
                )
                .where(AgentEvent.session_id == session_id)
                .order_by(AgentEvent.id)
            )
            return [tuple(row) for row in rows]

    async def llm_responses(self, session_id: str) -> list[tuple]:
        async with self.factory() as session:
            rows = await session.execute(
                select(AgentEvent.round_num, AgentEvent.status, AgentEvent.arguments_json)
                .where(AgentEvent.session_id == session_id, AgentEvent.event_type == "llm_response")
                .order_by(AgentEvent.round_num)
            )
            return [tuple(row) for row in rows]

    async def thread(self, session_id: str) -> list:
        return await get_thread_messages(
            session_id=session_id,
            session_factory=self.factory,
            settings=self.settings,
            checkpointer=self.checkpointer,
        )

    async def appointment(self, code: str) -> dict:
        async with self.factory() as session:
            row = await session.scalar(select(Appointment).where(Appointment.code == code))
            return {c.name: getattr(row, c.name) for c in Appointment.__table__.columns}


@pytest.fixture
async def world(agent_session_factory, checkpointer, monkeypatch):
    async def _db():
        async with agent_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_session_factory] = lambda: agent_session_factory
    app.dependency_overrides[get_checkpointer] = lambda: checkpointer
    monkeypatch.setattr(
        "app.api.routes.chat.get_default_chat_model",
        lambda: pytest.fail("the in-process model must not be built in harness mode"),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        harness = FakeHarness(http)
        monkeypatch.setattr("app.agent.harness_bridge._post_turn", harness)
        yield World(http, harness, agent_session_factory, checkpointer, monkeypatch)
    app.dependency_overrides.clear()
    async with agent_session_factory() as session:
        await session.execute(
            text("TRUNCATE agent_events, conversation_messages, agent_sessions CASCADE")
        )
        await session.commit()


async def test_happy_path_serves_tool_calls_and_writes_the_thread(world):
    world.harness.script = [SEARCH]

    body = await world.chat(ASK)

    tool_result = json.loads(world.harness.responses[0]["content"])
    assert (body["handled_by"], body["message"]) == ("agent", world.harness.text)
    assert body["terminated_reason"] is None
    assert body["touched_entity_codes"] == extract_entity_codes(tool_result)
    assert "APT-2001" in body["touched_entity_codes"]
    events = await world.events(body["session_id"])
    assert ("agent_invoked", None, EventStatus.SUCCESS, None) in events
    assert ("tool_executed", "search_appointments", EventStatus.SUCCESS, None) in events
    thread = await world.thread(body["session_id"])
    assert [m.type for m in thread] == ["system", "human", "ai", "tool", "ai"]
    assert thread[2].tool_calls[0]["name"] == "search_appointments"
    assert thread[3].tool_call_id == thread[2].tool_calls[0]["id"]
    assert thread[3].content == world.harness.responses[0]["content"]
    assert thread[4].content == world.harness.text
    assert harness_registry.get(world.harness.turn_id) is None


async def test_gateway_local_call_is_stored_with_its_result_and_never_served(world):
    world.harness.script = [LOCAL]

    body = await world.chat(ASK)

    assert world.harness.responses == []
    thread = await world.thread(body["session_id"])
    assert [m.type for m in thread] == ["system", "human", "ai", "tool", "ai"]
    assert thread[2].tool_calls[0]["name"] == "current_time"
    assert thread[3].content == LOCAL_RESULTS["current_time"]
    assert thread[4].content == world.harness.text
    events = await world.events(body["session_id"])
    assert not [e for e in events if e[0] == "tool_executed"]


async def test_each_reported_round_is_traced_with_its_tokens(world):
    world.harness.script = [SEARCH]

    body = await world.chat(ASK)

    assert await world.llm_responses(body["session_id"]) == [
        (1, EventStatus.SUCCESS, {"tool_call_count": 1, "input_tokens": 100, "output_tokens": 10}),
        (2, EventStatus.SUCCESS, {"input_tokens": 200, "output_tokens": 20}),
    ]


async def test_payload_limits_follow_the_settings(world):
    settings = world.configure(harness_max_tokens=1234, harness_turn_timeout_seconds=30)

    await world.chat(ASK)

    assert world.harness.payloads[0]["llm"] == {
        "model": settings.harness_model,
        "max_tokens": 1234,
        "deadline_in_ms": 30_000,
        "max_rounds": settings.max_agent_rounds,
    }


async def test_payload_system_carries_task_guidance_only_when_enabled(world):
    await world.chat("Admit the waiting patient to an available bed")
    world.configure(agent_skills_enabled=True)
    await world.chat("Admit the waiting patient to an available bed")
    await world.chat(ASK)

    plain, guided, unmatched = (p["system"] for p in world.harness.payloads)
    assert "Task guidance" not in plain
    assert "Bed allocation" in guided
    assert unmatched == plain


async def test_second_turn_history_carries_the_first_turns_tool_exchange(world):
    world.harness.script = [SEARCH]
    first = await world.chat(ASK)

    world.harness.script = []
    await world.chat(FOLLOW_UP, first["session_id"])

    payload = world.harness.payloads[1]
    history = payload["history"]
    assert payload["user"] == FOLLOW_UP
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    call = history[1]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"] == {
        "name": "search_appointments",
        "arguments": json.dumps(SEARCH[1]),
    }
    assert history[2]["tool_call_id"] == call["id"]
    assert history[3]["content"] == world.harness.text
    assert "system" not in {m["role"] for m in history}


async def test_ungrounded_reschedule_is_rejected_and_changes_nothing(world):
    before = await world.appointment("APT-2001")
    world.harness.script = [UNGROUNDED_RESCHEDULE]

    body = await world.chat("Move APT-2001 to scanner SCN-2 please")

    assert "never exposed" in world.harness.responses[0]["content"]
    assert world.harness.responses[0]["end_turn"] is None
    assert world.harness.turn.turn.invalid_call_count == 1
    assert await world.appointment("APT-2001") == before
    events = await world.events(body["session_id"])
    assert (
        "tool_executed",
        "reschedule_appointment",
        EventStatus.REJECTED,
        "grounding_rejected",
    ) in events


@pytest.mark.parametrize(
    ("limits", "script", "served", "reason", "reply"),
    [
        (
            {"max_invalid_tool_calls": 2},
            [UNKNOWN] * 3,
            1,
            "too_many_invalid_calls",
            "I stopped because too many tool calls were invalid.",
        ),
        (
            {"max_tool_calls": 2},
            [SEARCH] * 3,
            2,
            "max_tool_calls_exceeded",
            "I stopped because this session's tool-call limit was reached.",
        ),
    ],
)
async def test_backend_caps_end_the_turn_and_beat_the_harness_finish(
    world, limits, script, served, reason, reply
):
    world.configure(**limits)
    world.harness.script = script
    world.harness.text = "The model claims success."

    body = await world.chat(ASK)

    assert [r["end_turn"] for r in world.harness.responses] == [None] * served + [reason]
    assert (body["terminated_reason"], body["message"]) == (reason, reply)
    assert (await world.thread(body["session_id"]))[-1].content == reply


async def test_unreachable_harness_gives_the_fixed_reply_and_a_failure_event(world):
    world.harness.raises = httpx.ConnectError("secret connection detail")

    body = await world.chat(ASK)

    assert (body["handled_by"], body["terminated_reason"]) == ("agent", "harness_unavailable")
    assert body["message"] == UNAVAILABLE
    events = await world.events(body["session_id"])
    assert ("harness_unavailable", None, EventStatus.FAILURE, "harness_unavailable") in events
    assert await world.llm_responses(body["session_id"]) == []
    last = (await world.thread(body["session_id"]))[-1]
    assert isinstance(last, AIMessage) and last.content == UNAVAILABLE


async def test_harness_error_text_never_reaches_the_user(world):
    world.harness.stop_reason = "error"
    world.harness.error = "PROVIDER-LEAK sk-secret echoed prompt"

    response = await world.http.post("/api/chat", json={"text": ASK})

    assert response.json()["terminated_reason"] == "harness_error"
    assert response.json()["message"] == GENERIC_ERROR
    assert "PROVIDER-LEAK" not in response.text


@pytest.mark.parametrize(
    ("finish", "reason"),
    [
        ({"stop_reason": "round_limit"}, "max_rounds_exceeded"),
        ({"stop_reason": "time_limit"}, "llm_timeout"),
        ({"raw_finish": {"stop_reason": "bogus"}}, "harness_unavailable"),
    ],
)
async def test_harness_stop_reasons_and_malformed_finish_map_to_terminated_reasons(
    world, finish, reason
):
    for name, value in finish.items():
        setattr(world.harness, name, value)

    body = await world.chat(ASK)

    assert body["terminated_reason"] == reason
    assert body["message"] != world.harness.text
    assert reason in {event[0] for event in await world.events(body["session_id"])}


async def test_gates_and_parser_answer_without_the_harness(world):
    off_topic = await world.chat("What's the weather like today?")
    parsed = await world.chat("list departments")

    assert (off_topic["handled_by"], parsed["handled_by"]) == ("rejected", "deterministic")
    assert world.harness.payloads == []


async def test_in_process_runtime_never_reaches_the_harness(world):
    world.configure(agent_runtime="in_process", harness_token=None)
    final_state = {
        "messages": [AIMessage(content="From the in-process loop.")],
        "terminated_reason": None,
        "touched_entity_codes": [],
    }

    with (
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch("app.api.routes.chat.run_agent", new=AsyncMock(return_value=final_state)) as run,
    ):
        body = await world.chat(ASK)

    assert body["message"] == "From the in-process loop."
    run.assert_awaited_once()
    assert world.harness.payloads == []


async def test_internal_route_refuses_strangers_in_flight_and_everyone_afterwards(world):
    in_flight = {}

    async def probe(harness):
        wrong = await harness.call_tool(harness.turn_id, 1, SEARCH[0], "{}", token="x" * 32)
        in_flight["wrong_token"] = (wrong.status_code, wrong.json())
        right = await harness.call_tool(harness.turn_id, 1, SEARCH[0], "{}")
        in_flight["right_token"] = right.status_code

    world.harness.mid_turn = probe
    await world.chat(ASK)

    assert in_flight == {"wrong_token": (404, {"detail": "Not Found"}), "right_token": 200}
    finished = await world.harness.call_tool(world.harness.turn_id, 1, SEARCH[0], "{}")
    assert (finished.status_code, finished.json()) == (404, {"detail": "Not Found"})


MODEL_ERROR = {"stop_reason": "error", "error": "model_error"}
FALLBACK = "openai/gpt-fallback"


async def test_model_error_before_any_tool_call_is_retried_once_on_the_fallback(world):
    world.configure(harness_fallback_model=FALLBACK)
    world.harness.attempts = [MODEL_ERROR, {"text": "Answer from the fallback."}]

    body = await world.chat(ASK)

    first, second = world.harness.payloads
    assert (body["handled_by"], body["message"]) == ("agent", "Answer from the fallback.")
    assert body["terminated_reason"] is None
    assert first["llm"]["model"] == world.settings.harness_model
    assert second["llm"]["model"] == FALLBACK
    assert second["turn_id"] == first["turn_id"]
    events = await world.events(body["session_id"])
    assert ("model_fallback", None, EventStatus.SUCCESS, None) in events


async def test_model_error_after_a_served_tool_call_is_not_retried(world):
    world.configure(harness_fallback_model=FALLBACK)
    world.harness.script = [SEARCH]
    world.harness.attempts = [MODEL_ERROR]

    body = await world.chat(ASK)

    assert len(world.harness.payloads) == 1
    assert (body["terminated_reason"], body["message"]) == ("harness_error", GENERIC_ERROR)
    assert "model_fallback" not in {e[0] for e in await world.events(body["session_id"])}


async def test_model_error_without_a_fallback_model_is_not_retried(world):
    world.harness.attempts = [MODEL_ERROR]

    body = await world.chat(ASK)

    assert len(world.harness.payloads) == 1
    assert (body["terminated_reason"], body["message"]) == ("harness_error", GENERIC_ERROR)


async def test_a_failing_retry_is_final(world):
    world.configure(harness_fallback_model=FALLBACK)
    world.harness.attempts = [MODEL_ERROR, MODEL_ERROR]

    body = await world.chat(ASK)

    assert len(world.harness.payloads) == 2
    assert (body["terminated_reason"], body["message"]) == ("harness_error", GENERIC_ERROR)

"""End-to-end tests for the Jev fast path through /api/chat, plus the
/api/cost-comparison endpoint it feeds.

No live calls: the `fake_typesafe` fixture (tests/conftest.py) installs a
fake `typesafe_sdk` under the real package name, so the production module
runs for real against a controllable double. The database is real, as the
project's integration convention requires.

The load-bearing assertion in this file is `get_default_chat_model` never
being called on a confident match — patched the same way
`test_chat_api.py::test_off_topic_request_is_rejected_without_ever_invoking_the_llm`
does it. That is the entire point of the feature: if the model client is
still constructed, nothing was saved, and a test checking only `handled_by`
would not notice.
"""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from app.db.models.agent import EventStatus
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.session_repository import SessionRepository

# Imported at module level on purpose — `test_chat_api.py` does the same.
# `app/main.py` calls `asyncio.set_event_loop_policy(...)` at import time
# (for the Windows/psycopg reason documented there). If that import happens
# lazily inside the `client` fixture instead, it lands *after* `db_session`
# has already opened an asyncpg connection bound to the current loop, and
# swapping the policy mid-test makes that connection fail with "attached to
# a different loop". Importing here forces it to happen at collection time.
from app.main import app  # noqa: F401

pytestmark = pytest.mark.integration

# A message the regex parser cannot match, that passes the domain gate and
# the eligibility gate, and that a human would obviously route to
# `list_departments`. Exactly the gap this feature exists to close.
UNMATCHED_BUT_ROUTABLE = "what departments do you have?"

AGENT_FINAL_STATE = {
    "messages": [AIMessage(content="Here's what I found.")],
    "terminated_reason": None,
    "touched_entity_codes": [],
}


async def _trace(client, session_id: str) -> list[dict]:
    response = await client.get(f"/api/sessions/{session_id}/trace")
    return response.json()


def _jev_events(trace: list[dict]) -> list[dict]:
    return [e for e in trace if e["event_type"] == "jev_invoked"]


# --- (a) A confident match never reaches the agent ------------------------


async def test_confident_match_is_handled_by_jev_without_ever_invoking_the_llm(
    client, fake_typesafe, jev_settings, jev_response
):
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.98)

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model") as mock_get_model,
    ):
        response = await client.post("/api/chat", json={"text": UNMATCHED_BUT_ROUTABLE})

    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "jev"
    mock_get_model.assert_not_called()

    # Executed through the real CommandRunner against the real database —
    # the same results the deterministic branch produces for "list
    # departments", not a canned acknowledgment.
    assert len(body["data"]) == 4
    for department in body["data"]:
        assert department["name"] in body["message"]
    assert sorted(body["touched_entity_codes"]) == sorted(d["code"] for d in body["data"])


async def test_a_matched_turn_records_the_jev_trace_event_contract(
    client, fake_typesafe, jev_settings, jev_response
):
    """The frontend's trace panel is built against this exact shape."""
    fake_typesafe.behavior = jev_response(
        "list_scanners", confidence=0.95, filters={"scanner_type": ("MRI", 0.97)}
    )

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model"),
    ):
        response = await client.post(
            "/api/chat", json={"text": "which MRI machines do you have?"}
        )

    session_id = response.json()["session_id"]
    events = _jev_events(await _trace(client, session_id))
    assert len(events) == 1
    event = events[0]

    assert event["event_type"] == "jev_invoked"
    assert event["tool_name"] == "list_scanners"
    assert event["status"] == EventStatus.SUCCESS.value
    assert event["round_num"] == 0
    assert event["error_category"] is None
    assert event["latency_ms"] is not None

    assert set(event["arguments_json"]) == {
        "choice",
        "confidence",
        "probabilities",
        "threshold",
        "input_tokens",
        "output_tokens",
        "model",
    }
    assert event["arguments_json"]["choice"] == "list_scanners"
    assert event["arguments_json"]["confidence"] == pytest.approx(0.95)
    assert event["arguments_json"]["threshold"] == pytest.approx(0.9)
    assert event["arguments_json"]["input_tokens"] == 350
    assert event["arguments_json"]["model"] == "jev-latest"


async def test_a_jev_handled_turn_is_persisted_to_conversation_history(
    client, fake_typesafe, jev_settings, jev_response
):
    """Every branch of /api/chat must append to ConversationMessage so the
    next turn has continuity regardless of which path handled this one."""
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.98)

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model"),
    ):
        response = await client.post("/api/chat", json={"text": UNMATCHED_BUT_ROUTABLE})

    session_id = response.json()["session_id"]
    messages = (await client.get(f"/api/sessions/{session_id}/messages")).json()
    assert [m["role"] for m in messages] == ["USER", "ASSISTANT"]
    assert messages[1]["content"] == response.json()["message"]


# --- (b) An unconfident answer still reaches the agent --------------------


async def test_an_unconfident_answer_falls_through_to_the_agent(
    client, fake_typesafe, jev_settings, jev_response
):
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.55)

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch("app.api.routes.chat.run_agent", new=AsyncMock(return_value=AGENT_FINAL_STATE)) as run,
    ):
        response = await client.post("/api/chat", json={"text": UNMATCHED_BUT_ROUTABLE})

    body = response.json()
    assert body["handled_by"] == "agent"
    run.assert_awaited_once()

    # Consulted-but-declined is still recorded, so the trace panel shows Jev
    # was asked even when it changed nothing.
    events = _jev_events(await _trace(client, body["session_id"]))
    assert len(events) == 1
    assert events[0]["status"] == EventStatus.REJECTED.value
    assert events[0]["tool_name"] is None
    assert events[0]["error_category"] is None


async def test_a_none_choice_falls_through_to_the_agent(
    client, fake_typesafe, jev_settings, jev_response
):
    fake_typesafe.behavior = jev_response("none", confidence=0.99)

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch("app.api.routes.chat.run_agent", new=AsyncMock(return_value=AGENT_FINAL_STATE)),
    ):
        response = await client.post(
            "/api/chat", json={"text": "can you move my MRI appointment to tomorrow?"}
        )

    body = response.json()
    assert body["handled_by"] == "agent"
    events = _jev_events(await _trace(client, body["session_id"]))
    assert events[0]["status"] == EventStatus.REJECTED.value


# --- (c) A Jev failure degrades gracefully --------------------------------


async def test_a_jev_api_failure_still_reaches_the_agent(
    client, fake_typesafe, jev_settings
):
    """A Jev outage must degrade this feature back to today's behavior — not
    fail the chat turn."""
    fake_typesafe.behavior = fake_typesafe.module.TypeSafeRateLimitError("slow down")

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch("app.api.routes.chat.run_agent", new=AsyncMock(return_value=AGENT_FINAL_STATE)) as run,
    ):
        response = await client.post("/api/chat", json={"text": UNMATCHED_BUT_ROUTABLE})

    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "agent"
    run.assert_awaited_once()

    events = _jev_events(await _trace(client, body["session_id"]))
    assert events[0]["status"] == EventStatus.FAILURE.value
    assert events[0]["error_category"] == "jev_api_error"
    assert events[0]["arguments_json"]["choice"] is None


async def test_a_missing_sdk_still_reaches_the_agent(client, jev_settings, monkeypatch):
    """Deliberately no `fake_typesafe` here: with the flag on but the package
    not installed, the turn must still complete normally."""
    import builtins
    import sys

    real_import = builtins.__import__

    def _no_typesafe(name, *args, **kwargs):
        if name == "typesafe_sdk":
            raise ImportError("No module named 'typesafe_sdk'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "typesafe_sdk", raising=False)
    monkeypatch.setattr(builtins, "__import__", _no_typesafe)

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch("app.api.routes.chat.run_agent", new=AsyncMock(return_value=AGENT_FINAL_STATE)),
    ):
        response = await client.post("/api/chat", json={"text": UNMATCHED_BUT_ROUTABLE})

    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "agent"
    events = _jev_events(await _trace(client, body["session_id"]))
    assert events[0]["status"] == EventStatus.FAILURE.value
    assert events[0]["error_category"] == "jev_sdk_missing"


# --- (d) Flag off: byte-for-byte inert ------------------------------------


async def test_with_the_flag_off_no_client_is_constructed_and_the_agent_runs(
    client, fake_typesafe, jev_settings
):
    """The whole feature must be invisible when disabled: no client built, no
    call made, no trace event written, and the request routed exactly as it
    is on master."""
    with (
        patch(
            "app.api.routes.chat.get_settings",
            return_value=jev_settings(enable_jev_fast_path=False),
        ),
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch("app.api.routes.chat.run_agent", new=AsyncMock(return_value=AGENT_FINAL_STATE)) as run,
    ):
        response = await client.post("/api/chat", json={"text": UNMATCHED_BUT_ROUTABLE})

    body = response.json()
    assert body["handled_by"] == "agent"
    run.assert_awaited_once()
    assert fake_typesafe.client_constructions == 0
    assert fake_typesafe.calls == []
    assert _jev_events(await _trace(client, body["session_id"])) == []


async def test_with_the_flag_off_the_deterministic_path_is_unchanged(
    client, fake_typesafe, jev_settings
):
    with patch(
        "app.api.routes.chat.get_settings",
        return_value=jev_settings(enable_jev_fast_path=False),
    ):
        response = await client.post("/api/chat", json={"text": "list departments"})

    assert response.json()["handled_by"] == "deterministic"
    assert fake_typesafe.client_constructions == 0


async def test_the_domain_gate_still_runs_before_jev(
    client, fake_typesafe, jev_settings, jev_response
):
    """Ordering is the non-negotiable part of the wiring: an off-topic
    message must be rejected for free by the domain gate, never spend a Jev
    call. A regression that moved the fast path above the gate would show up
    here as a recorded Jev call."""
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.99)

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model") as mock_get_model,
    ):
        response = await client.post(
            "/api/chat", json={"text": "what's the weather like today?"}
        )

    assert response.json()["handled_by"] == "rejected"
    mock_get_model.assert_not_called()
    assert fake_typesafe.client_constructions == 0


async def test_the_eligibility_gate_still_runs_before_jev(
    client, fake_typesafe, jev_settings, jev_response
):
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.99)

    with patch("app.api.routes.chat.get_settings", return_value=jev_settings()):
        response = await client.post("/api/chat", json={"text": "   "})

    assert response.json()["handled_by"] == "rejected"
    assert fake_typesafe.client_constructions == 0


# --- /api/cost-comparison -------------------------------------------------


async def _seed_jev_events(session, session_id: str, specs: list[tuple]) -> None:
    """specs: (status, tool_name, input_tokens|None, error_category|None)."""
    await SessionRepository(session).get_or_create(session_id)
    repo = EventRepository(session)
    for status, tool_name, input_tokens, error_category in specs:
        await repo.record(
            session_id=session_id,
            round_num=0,
            event_type="jev_invoked",
            status=status,
            tool_name=tool_name,
            arguments={
                "choice": tool_name,
                "confidence": 0.99 if tool_name else None,
                "probabilities": None,
                "threshold": 0.9,
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "model": "jev-latest",
            },
            latency_ms=120,
            error_category=error_category,
        )
    await session.commit()


async def test_cost_comparison_with_no_events_is_all_zeroes(client):
    body = (await client.get("/api/cost-comparison")).json()
    assert body["jev_calls_total"] == 0
    assert body["jev_fast_path_hits"] == 0
    assert body["net_savings_usd"] == 0.0
    # The assumptions are echoed even when there's nothing to compare, so the
    # UI can always show its work.
    assert body["assumptions"]["sonnet_input_usd_per_mtok"] == 2.0


async def test_cost_comparison_aggregates_hits_and_fallthroughs(client, seeded_session):
    await _seed_jev_events(
        seeded_session,
        "cost-session-1",
        [
            (EventStatus.SUCCESS, "list_departments", 400, None),
            (EventStatus.SUCCESS, "list_scanners", 420, None),
            (EventStatus.REJECTED, None, 380, None),
            (EventStatus.FAILURE, None, None, "jev_timeout"),
        ],
    )

    body = (await client.get("/api/cost-comparison")).json()

    assert body["jev_calls_total"] == 4
    assert body["jev_fast_path_hits"] == 2
    assert body["jev_fallthroughs"] == 2
    assert body["jev_failures"] == 1
    # The failed call contributes no tokens — see the endpoint's constants.
    assert body["jev_input_tokens"] == 400 + 420 + 380

    assert body["avoided_agent_turns"] == 2
    # 2 turns x (1900 in @ $2/M + 120 out @ $10/M) = 2 x $0.005
    assert body["avoided_sonnet_cost_usd"] == pytest.approx(0.01)
    # 1200 tokens @ $0.042/M
    assert body["jev_cost_usd"] == pytest.approx(0.0000504, abs=1e-9)
    assert body["net_savings_usd"] == pytest.approx(0.01 - 0.0000504, abs=1e-9)
    assert body["net_savings_usd"] > 0


async def test_cost_comparison_falls_back_to_the_documented_estimate_for_null_tokens(
    client, seeded_session
):
    """`usage.input_tokens` is `int | None` in the SDK contract. A completed
    call with null usage uses the documented estimate rather than silently
    counting as free."""
    await _seed_jev_events(
        seeded_session,
        "cost-session-null",
        [(EventStatus.SUCCESS, "list_departments", None, None)],
    )

    body = (await client.get("/api/cost-comparison")).json()

    assert body["jev_input_tokens"] == body["assumptions"]["estimated_jev_input_tokens_per_call"]


async def test_cost_comparison_can_be_scoped_to_one_session(client, seeded_session):
    await _seed_jev_events(
        seeded_session,
        "cost-session-a",
        [(EventStatus.SUCCESS, "list_departments", 400, None)],
    )
    await _seed_jev_events(
        seeded_session,
        "cost-session-b",
        [
            (EventStatus.SUCCESS, "list_scanners", 400, None),
            (EventStatus.REJECTED, None, 400, None),
        ],
    )

    scoped = (await client.get("/api/cost-comparison?session_id=cost-session-b")).json()
    assert scoped["session_id"] == "cost-session-b"
    assert scoped["jev_calls_total"] == 2
    assert scoped["jev_fast_path_hits"] == 1

    everything = (await client.get("/api/cost-comparison")).json()
    assert everything["session_id"] is None
    assert everything["jev_calls_total"] == 3


async def test_cost_comparison_ignores_non_jev_events(client, seeded_session):
    await SessionRepository(seeded_session).get_or_create("cost-session-mixed")
    await EventRepository(seeded_session).record(
        session_id="cost-session-mixed",
        round_num=1,
        event_type="tool_call",
        status=EventStatus.SUCCESS,
        tool_name="search_appointments",
    )
    await seeded_session.commit()

    body = (await client.get("/api/cost-comparison")).json()
    assert body["jev_calls_total"] == 0


# --- Live-gated (skipped by default; never run in this session) -----------


@pytest.mark.live_llm
async def test_live_jev_routes_a_real_message(jev_settings):
    """One real Jev round trip, gated exactly like the project's other live
    tests: skipped unless both RUN_LIVE_LLM_TESTS=1 and a real
    TYPESAFE_API_KEY are present.

    Run explicitly with:
      RUN_LIVE_LLM_TESTS=1 pytest -m live_llm tests/e2e/test_jev_fast_path_api.py
    """
    from app.agent.jev_fast_path import try_jev_fast_path
    from app.config import get_settings

    settings = get_settings()
    if not settings.run_live_llm_tests:
        pytest.skip("RUN_LIVE_LLM_TESTS is not set to 1")
    if not settings.typesafe_api_key:
        pytest.skip("TYPESAFE_API_KEY is not set")

    result = await try_jev_fast_path(
        "what departments do you have?",
        jev_settings(typesafe_api_key=settings.typesafe_api_key),
    )

    assert result.failure_reason != "jev_sdk_missing"
    assert result.choice is not None
    print(f"\nLive Jev decision: {result.choice} @ {result.confidence}")


@pytest.mark.live_llm
async def test_live_jev_declines_an_unroutable_message(jev_settings):
    """The other half of the live sanity check: a request that is genuinely
    the agent's job must come back as a non-match, not be forced onto a
    command. Cheap (one call) and the more important of the two directions."""
    from app.agent.jev_fast_path import try_jev_fast_path
    from app.config import get_settings

    settings = get_settings()
    if not settings.run_live_llm_tests:
        pytest.skip("RUN_LIVE_LLM_TESTS is not set to 1")
    if not settings.typesafe_api_key:
        pytest.skip("TYPESAFE_API_KEY is not set")

    result = await try_jev_fast_path(
        "move the first delayed MRI appointment onto an available scanner",
        jev_settings(typesafe_api_key=settings.typesafe_api_key),
    )

    assert result.matched is False

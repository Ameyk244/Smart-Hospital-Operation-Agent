"""The agent's watch and alert tools: each runs the scheduler command through
CommandRunner, and the two that act on an existing row require it be grounded."""

import uuid

import pytest
from pydantic import ValidationError
import pytest_asyncio
from sqlalchemy import delete, select

from app.agent.grounding import GroundingRejectedError
from app.agent.graph import SYSTEM_PROMPT, WATCH_PROMPT, build_system_message
from app.agent.tools import TOOL_REGISTRY, all_tool_dicts, get_tool
from app.agent.tools.base import ToolExecutionError
from app.agent.tools.scheduler_tools import (
    AcknowledgeAlertArgs,
    CreateWatchArgs,
    ListAlertsArgs,
    NoArgs,
    StopWatchArgs,
    handle_acknowledge_alert,
    handle_create_watch,
    handle_list_alerts,
    handle_list_watches,
    handle_stop_watch,
)
from app.agent.turn import ToolTurn
from app.config import get_settings
from app.db.models.scheduler import Alert, AlertSeverity, ScheduledTask
from app.db.repositories.session_repository import SessionRepository
from tests.integration.test_agent_loop import _settings

pytestmark = pytest.mark.integration

WATCH = {"condition": "FREE_BEDS_UNDER", "threshold": 2}


@pytest.fixture(autouse=True)
def scheduler_on(monkeypatch):
    settings = get_settings().model_copy(update={"scheduler_enabled": True})
    monkeypatch.setattr("app.execution.commands.scheduler_commands.get_settings", lambda: settings)


async def _session_id(session) -> str:
    session_id = f"test-sched-{uuid.uuid4().hex[:8]}"
    await SessionRepository(session).get_or_create(session_id)
    return session_id


async def _alert(session, task_code: str) -> int:
    task = await session.scalar(select(ScheduledTask).where(ScheduledTask.code == task_code))
    alert = Alert(task_id=task.id, dedupe_key="k", severity=AlertSeverity.WARNING, summary="s", entity_codes=[])
    session.add(alert)
    await session.flush()
    return alert.id


def test_registry_flags_the_five_tools():
    for name, is_write in [
        ("list_watches", False),
        ("list_alerts", False),
        ("create_watch", True),
        ("stop_watch", True),
        ("acknowledge_alert", True),
    ]:
        assert TOOL_REGISTRY[name].is_write is is_write


def test_flag_off_offers_the_original_14_tools_and_flag_on_all_19():
    off, on = _settings(scheduler_enabled=False), _settings(scheduler_enabled=True)
    assert len(all_tool_dicts(off)) == 14
    assert len(all_tool_dicts(on)) == 19
    assert get_tool("create_watch", off) is None
    assert get_tool("create_watch", on) is TOOL_REGISTRY["create_watch"]


async def test_prompt_gains_the_watch_sentence_only_when_enabled(agent_session_factory):
    off = await build_system_message(agent_session_factory, "p-off", _settings(scheduler_enabled=False))
    on = await build_system_message(agent_session_factory, "p-on", _settings(scheduler_enabled=True))
    assert off.content == SYSTEM_PROMPT
    assert on.content == SYSTEM_PROMPT + WATCH_PROMPT


async def test_disabled_tool_call_is_rejected_as_unknown(agent_session_factory):
    async with agent_session_factory() as session:
        await SessionRepository(session).get_or_create("disabled-tool")
        await session.commit()
    turn = ToolTurn(
        session_id="disabled-tool",
        session_factory=agent_session_factory,
        settings=_settings(scheduler_enabled=False),
    )
    assert await turn.call(name="create_watch", args=WATCH, round_num=1) == "Unknown tool 'create_watch'"


def test_alert_status_is_upper_case():
    with pytest.raises(ValidationError):
        ListAlertsArgs(status="open")


async def test_create_watch_grounds_the_new_watch_for_stop_watch(seeded_session):
    sid = await _session_id(seeded_session)
    created = await handle_create_watch(seeded_session, sid, CreateWatchArgs(**WATCH))
    stopped = await handle_stop_watch(seeded_session, sid, StopWatchArgs(task_code=created["code"]))
    assert stopped["active"] is False


async def test_list_alerts_returns_at_most_the_50_newest(seeded_session):
    sid = await _session_id(seeded_session)
    task = await handle_create_watch(seeded_session, sid, CreateWatchArgs(**WATCH))
    ids = [await _alert(seeded_session, task["code"]) for _ in range(52)]
    listed = await handle_list_alerts(seeded_session, sid, ListAlertsArgs(status="ALL"))
    assert [a["id"] for a in listed] == sorted(ids, reverse=True)[:50]


async def test_watch_lifecycle_through_the_tools(seeded_session):
    sid = await _session_id(seeded_session)
    created = await handle_create_watch(seeded_session, sid, CreateWatchArgs(**WATCH, ward_code="WRD-1"))
    assert (created["condition"], created["scope_code"]) == ("FREE_BEDS_UNDER", "WRD-1")

    watches = await handle_list_watches(seeded_session, sid, NoArgs())
    assert created["code"] in [w["code"] for w in watches]

    stopped = await handle_stop_watch(seeded_session, sid, StopWatchArgs(task_code=created["code"]))
    assert stopped["active"] is False


async def test_alert_lifecycle_through_the_tools(seeded_session):
    sid = await _session_id(seeded_session)
    task = await handle_create_watch(seeded_session, sid, CreateWatchArgs(**WATCH))
    alert_id = await _alert(seeded_session, task["code"])

    open_alerts = await handle_list_alerts(seeded_session, sid, ListAlertsArgs())
    assert alert_id in [a["id"] for a in open_alerts]

    done = await handle_acknowledge_alert(seeded_session, sid, AcknowledgeAlertArgs(alert_id=alert_id))
    assert done["acknowledged_at"] is not None
    assert alert_id not in [a["id"] for a in await handle_list_alerts(seeded_session, sid, ListAlertsArgs())]
    assert alert_id in [a["id"] for a in await handle_list_alerts(seeded_session, sid, ListAlertsArgs(status="ALL"))]


async def test_stop_and_acknowledge_reject_ungrounded_values(seeded_session):
    sid = await _session_id(seeded_session)
    task = await handle_create_watch(seeded_session, sid, CreateWatchArgs(**WATCH))
    alert_id = await _alert(seeded_session, task["code"])

    with pytest.raises(GroundingRejectedError):
        await handle_stop_watch(seeded_session, await _session_id(seeded_session), StopWatchArgs(task_code=task["code"]))
    with pytest.raises(GroundingRejectedError):
        await handle_acknowledge_alert(seeded_session, sid, AcknowledgeAlertArgs(alert_id=alert_id))


async def test_command_errors_become_tool_errors(seeded_session):
    sid = await _session_id(seeded_session)
    await handle_create_watch(seeded_session, sid, CreateWatchArgs(**WATCH))
    with pytest.raises(ToolExecutionError) as dup:
        await handle_create_watch(seeded_session, sid, CreateWatchArgs(**WATCH))
    assert dup.value.category == "invalid_argument"
    with pytest.raises(ToolExecutionError) as ward:
        await handle_create_watch(
            seeded_session, sid, CreateWatchArgs(condition="QUEUE_WAIT_OVER", threshold=5, ward_code="WRD-1")
        )
    assert ward.value.category == "invalid_argument"


async def test_every_tool_reports_the_scheduler_is_off(seeded_session, monkeypatch):
    off = get_settings().model_copy(update={"scheduler_enabled": False})
    monkeypatch.setattr("app.execution.commands.scheduler_commands.get_settings", lambda: off)
    sid = await _session_id(seeded_session)
    for handler, args in (
        (handle_create_watch, CreateWatchArgs(**WATCH)),
        (handle_list_watches, NoArgs()),
        (handle_list_alerts, ListAlertsArgs()),
    ):
        with pytest.raises(ToolExecutionError) as err:
            await handler(seeded_session, sid, args)
        assert err.value.category == "scheduler_off"


@pytest_asyncio.fixture
async def clean_scheduler_rows(agent_session_factory):
    yield
    async with agent_session_factory() as session:
        await session.execute(delete(Alert))
        await session.execute(delete(ScheduledTask))
        await session.commit()


async def test_write_cap_counts_the_scheduler_writes(agent_session_factory, clean_scheduler_rows):
    session_id = f"test-sched-cap-{uuid.uuid4().hex[:8]}"
    async with agent_session_factory() as session:
        await SessionRepository(session).get_or_create(session_id)
        await session.commit()
    turn = ToolTurn(
        session_id=session_id,
        session_factory=agent_session_factory,
        settings=_settings(max_write_calls_per_turn=1, scheduler_enabled=True),
    )
    limit = "Not executed: this turn's write-call limit was reached."

    created = await turn.call(name="create_watch", args=WATCH, round_num=1)
    stop = await turn.call(name="stop_watch", args={"task_code": "TSK-1"}, round_num=1)
    ack = await turn.call(name="acknowledge_alert", args={"alert_id": 1}, round_num=1)
    again = await turn.call(name="create_watch", args={**WATCH, "threshold": 3}, round_num=1)

    assert "TSK-" in created
    assert (stop, ack, again) == (limit, limit, limit)
    assert turn.write_call_count == 1

"""A real agent turn involving a patient's name must not leak that name into
the stored trace (`agent_events`, which the trace panel shows) or into log
output (observability Phase 1).

A scripted model (no live call) makes three calls that each carry the name:
- a valid `execute_command("show patient <name>")`, which really runs;
- a `search_appointments` with the name stuffed into `patient_code` (it
  validates as a string, runs, and finds nothing);
- a `search_appointments` whose `limit` is the name, which fails validation.
  That is the `invalid_argument` path, which stores the model's raw args.
"""

import json
import uuid

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.agent.graph import run_agent
from app.config import Settings
from app.db.models.agent import EventStatus
from app.db.models.hospital import Patient
from app.db.repositories.event_repository import EventRepository

pytestmark = pytest.mark.integration


class ScriptedChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
    )


async def test_patient_name_never_reaches_trace_or_logs(agent_session_factory, capsys):
    async with agent_session_factory() as s:
        patient = (await s.execute(select(Patient).order_by(Patient.id).limit(1))).scalar_one()
    name = patient.name
    surname = name.split()[-1]

    model = ScriptedChatModel(
        responses=[
            _call("execute_command", {"command_text": f"show patient {name}"}, "c1"),
            _call("search_appointments", {"patient_code": name}, "c2"),
            _call("search_appointments", {"limit": name}, "c3"),
            AIMessage(content="Done."),
        ]
    )
    session_id = f"test-redaction-{uuid.uuid4().hex[:8]}"
    await run_agent(
        session_id=session_id,
        user_text=f"show me {name}",
        chat_model=model,
        session_factory=agent_session_factory,
        settings=Settings(
            database_url="postgresql+asyncpg://unused/unused",
            max_agent_rounds=6,
            max_tool_calls=10,
            tool_timeout_seconds=5,
            llm_timeout_seconds=5,
            max_invalid_tool_calls=5,
        ),
    )

    async with agent_session_factory() as s:
        events = await EventRepository(s).list_for_session(session_id)
    tool_events = [e for e in events if e.event_type in ("tool_executed", "tool_requested")]
    assert len(tool_events) == 3

    # The lookup genuinely ran: this is the path where a name would have been stored.
    executed = next(e for e in tool_events if e.tool_name == "execute_command")
    assert executed.status == EventStatus.SUCCESS
    assert executed.arguments_json == {"command_text": "[REDACTED]"}
    invalid = next(e for e in tool_events if e.error_category == "invalid_argument")
    assert invalid.arguments_json == {"limit": "[REDACTED]"}

    stored = json.dumps(
        [(e.tool_name, e.arguments_json, e.error_category) for e in events], default=str
    )
    assert name not in stored
    assert surname not in stored

    out = capsys.readouterr().out
    assert session_id in out  # the trace log lines were captured
    assert name not in out
    assert surname not in out

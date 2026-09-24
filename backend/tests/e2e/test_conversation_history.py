"""One conversation, whichever path answered each turn.

With the production checkpointer active, the graph state is the only history
the agent sees, and it used to hold agent-handled turns only. A question the
parser or Jev answered was invisible to the agent, so "what did you just
find?" after `list delayed appointments` got "I haven't run any searches
yet". Found by a real spoken test pass, not by the suite: every other e2e test
runs with `checkpointer=None`, where the transcript-based fallback hides the
bug. These tests use the real Postgres checkpointer on purpose.

No live calls: the agent model is a scripted fake that records exactly which
messages it was shown.
"""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from app.api.routes.chat import get_checkpointer
from app.db.session import get_db, get_session_factory
from app.main import app
from tests.integration.test_agent_loop import ScriptedChatModel

pytestmark = pytest.mark.integration


class RecordingModel(ScriptedChatModel):
    """Scripted model that remembers every message list it was called with."""

    seen: list = []

    def _generate(self, messages, *args, **kwargs):
        type(self).seen = [(m.type, m.content) for m in messages]
        return super()._generate(messages, *args, **kwargs)


@pytest.fixture
async def http(agent_session_factory, checkpointer):
    async def _db():
        async with agent_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_session_factory] = lambda: agent_session_factory
    app.dependency_overrides[get_checkpointer] = lambda: checkpointer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _agent_model(reply: str = "You just looked at delayed appointments.") -> RecordingModel:
    RecordingModel.seen = []
    return RecordingModel(responses=[AIMessage(content=reply)])


async def test_agent_sees_a_turn_the_parser_answered(http, jev_settings, fake_typesafe, jev_response):
    fake_typesafe.behavior = jev_response("none", confidence=1.0)
    first = await http.post("/api/chat", json={"text": "list delayed appointments"})
    assert first.json()["handled_by"] == "deterministic"
    session_id = first.json()["session_id"]

    model = _agent_model()
    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=model),
    ):
        second = await http.post(
            "/api/chat", json={"text": "what did you just find?", "session_id": session_id}
        )

    assert second.json()["handled_by"] == "agent"
    seen = [content for _kind, content in RecordingModel.seen]
    assert "list delayed appointments" in seen
    assert first.json()["message"] in seen
    # One system prompt, first: writing the parser turn must not leave the
    # thread without one or add a second.
    kinds = [kind for kind, _ in RecordingModel.seen]
    assert kinds[0] == "system" and kinds.count("system") == 1
    assert seen.index("list delayed appointments") < seen.index("what did you just find?")


async def test_agent_sees_a_turn_jev_answered(http, jev_settings, fake_typesafe, jev_response):
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.98)
    with patch("app.api.routes.chat.get_settings", return_value=jev_settings()):
        first = await http.post("/api/chat", json={"text": "what departments do you have?"})
    assert first.json()["handled_by"] == "jev"
    session_id = first.json()["session_id"]

    fake_typesafe.behavior = jev_response("none", confidence=1.0)
    model = _agent_model("Those were the four departments.")
    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=model),
    ):
        second = await http.post(
            "/api/chat", json={"text": "what did you just find?", "session_id": session_id}
        )

    assert second.json()["handled_by"] == "agent"
    seen = [content for _kind, content in RecordingModel.seen]
    assert "what departments do you have?" in seen
    assert first.json()["message"] in seen


async def test_turns_from_all_paths_stay_in_order(http, jev_settings, fake_typesafe, jev_response):
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.98)
    with patch("app.api.routes.chat.get_settings", return_value=jev_settings()):
        a = await http.post("/api/chat", json={"text": "list scanners mri"})
        sid = a.json()["session_id"]
        await http.post("/api/chat", json={"text": "what departments do you have?", "session_id": sid})

    fake_typesafe.behavior = jev_response("none", confidence=1.0)
    model = _agent_model()
    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=model),
    ):
        await http.post("/api/chat", json={"text": "why did that happen?", "session_id": sid})

    humans = [c for kind, c in RecordingModel.seen if kind == "human"]
    assert humans == ["list scanners mri", "what departments do you have?", "why did that happen?"]


async def test_a_rejected_turn_is_not_added_to_agent_history(
    http, jev_settings, fake_typesafe, jev_response
):
    """Only answered hospital turns are worth the agent's context. A rejected
    message (and its canned reply, which itself contains hospital words) must
    not become history the agent reasons over."""
    first = await http.post("/api/chat", json={"text": "what is the weather today?"})
    assert first.json()["handled_by"] == "rejected"
    sid = first.json()["session_id"]

    fake_typesafe.behavior = jev_response("none", confidence=1.0)
    model = _agent_model()
    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model", return_value=model),
    ):
        await http.post(
            "/api/chat", json={"text": "which scanners are in maintenance?", "session_id": sid}
        )
    contents = [c for _k, c in RecordingModel.seen]
    assert "what is the weather today?" not in contents


async def test_a_checkpoint_write_failure_does_not_fail_the_answered_request(
    http, jev_settings, fake_typesafe, jev_response
):
    with patch(
        "app.api.routes.chat.record_non_agent_turn", side_effect=RuntimeError("checkpoint down")
    ):
        response = await http.post("/api/chat", json={"text": "list departments"})
    assert response.status_code == 200
    assert response.json()["handled_by"] == "deterministic"
    assert len(response.json()["data"]) == 4

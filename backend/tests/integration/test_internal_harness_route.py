"""The internal tool-call route: token-first auth that is indistinguishable
from an unknown path, body validation, and delegation to the registered turn.
No database: the stub turn stands in for `ToolTurn`."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.agent import harness_registry
from app.config import Settings
from app.main import app

pytestmark = pytest.mark.integration

TOKEN = "t" * 32
HEADERS = {"x-harness-token": TOKEN}
BAD_HEADERS = {"x-harness-token": "x" * 32}
TURN_ID = "a" * 32
URL = f"/internal/v1/turns/{TURN_ID}/tool_calls"
BODY = {"round": 2, "call_id": "call_9", "name": "search_appointments", "arguments": "{}"}
NOT_FOUND = {"detail": "Not Found"}


class StubTurn:
    def __init__(self) -> None:
        self.terminated_reason: str | None = None
        self.calls: list[dict] = []

    async def call(self, *, call_id, name, args, round_num, parent_context=None) -> str:
        self.calls.append(
            {"call_id": call_id, "name": name, "args": args, "round_num": round_num}
        )
        return "result"


@pytest.fixture
def stub_turn(monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.internal_harness.get_settings",
        lambda: Settings(agent_runtime="harness", harness_token=TOKEN),
    )
    turn = StubTurn()
    harness_registry.register(TURN_ID, turn)
    yield turn
    harness_registry.unregister(TURN_ID)


@pytest_asyncio.fixture
async def http():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def test_wrong_token_is_indistinguishable_from_an_unknown_path(http, stub_turn):
    unknown = await http.post("/internal/v1/nothing", json=BODY)

    wrong = await http.post(URL, json=BODY, headers=BAD_HEADERS)

    assert (wrong.status_code, wrong.json()) == (unknown.status_code, unknown.json())
    assert wrong.json() == NOT_FOUND
    assert stub_turn.calls == []


async def test_missing_token_is_404(http, stub_turn):
    response = await http.post(URL, json=BODY)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_unset_token_setting_refuses_everything(http, stub_turn, monkeypatch):
    monkeypatch.setattr("app.api.routes.internal_harness.get_settings", lambda: Settings())

    response = await http.post(URL, json=BODY, headers={"x-harness-token": ""})

    assert response.status_code == 404


async def test_bad_token_with_malformed_body_is_still_404_not_422(http, stub_turn):
    response = await http.post(URL, content=b"{not json", headers=BAD_HEADERS)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_valid_token_with_malformed_body_is_422(http, stub_turn):
    response = await http.post(URL, content=b"{not json", headers=HEADERS)

    assert response.status_code == 422


async def test_out_of_range_round_is_422(http, stub_turn):
    response = await http.post(URL, json={**BODY, "round": 0}, headers=HEADERS)

    assert response.status_code == 422


async def test_unknown_turn_is_404(http, stub_turn):
    response = await http.post(
        f"/internal/v1/turns/{'b' * 32}/tool_calls", json=BODY, headers=HEADERS
    )

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_valid_call_returns_the_turn_content(http, stub_turn):
    response = await http.post(
        URL,
        json={**BODY, "arguments": '{"scanner_code": "SCN-3"}', "extra": "ignored"},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert response.json() == {"content": "result", "end_turn": None}
    assert stub_turn.calls == [
        {
            "call_id": "call_9",
            "name": "search_appointments",
            "args": {"scanner_code": "SCN-3"},
            "round_num": 2,
        }
    ]


@pytest.mark.parametrize("arguments", ['{"scanner_code": "SCN-3', "[1, 2]", "null"])
async def test_unparseable_or_non_object_arguments_reach_the_turn_as_none(
    http, stub_turn, arguments
):
    response = await http.post(URL, json={**BODY, "arguments": arguments}, headers=HEADERS)

    assert response.status_code == 200
    assert stub_turn.calls[0]["args"] is None


async def test_end_turn_reflects_the_turns_terminated_reason(http, stub_turn):
    stub_turn.terminated_reason = "max_tool_calls_exceeded"

    response = await http.post(URL, json=BODY, headers=HEADERS)

    assert response.json()["end_turn"] == "max_tool_calls_exceeded"

"""Validates the shared contract fixtures (docs/harness-smart-hospital-operations.md
sections 5 and 6) against strict models. The harness side is built against the
same files, so a shape change must start here."""

import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes.internal_harness import ToolCallRequest

FIXTURES = Path(__file__).parent.parent / "fixtures" / "harness_contract"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FunctionCall(Strict):
    name: str
    arguments: str


class HistoryToolCall(Strict):
    id: str
    type: Literal["function"]
    function: FunctionCall


class HistoryMessage(Strict):
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[HistoryToolCall] | None = None
    tool_call_id: str | None = None


class ToolFunction(Strict):
    name: str
    description: str
    parameters: dict


class ToolDefinition(Strict):
    type: Literal["function"]
    function: ToolFunction


class LlmSettings(Strict):
    model: str
    max_tokens: int
    deadline_in_ms: int
    max_rounds: int


class TurnRequest(Strict):
    turn_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    system: str
    history: list[HistoryMessage]
    user: str
    tools: list[ToolDefinition]
    llm: LlmSettings


class Usage(Strict):
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int


class RoundToolCall(Strict):
    id: str
    name: str
    arguments: str


class AssistantRound(Strict):
    round: int
    text: str
    tool_calls: list[RoundToolCall]
    usage: Usage


class TurnResponse(Strict):
    turn_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    stop_reason: Literal["round_limit", "time_limit", "error"] | None
    error: str | None = Field(max_length=2000)
    model_text: str
    rounds: int
    usage: Usage
    assistant_rounds: list[AssistantRound]


class ToolCallResponse(Strict):
    content: str
    end_turn: Literal["max_tool_calls_exceeded", "too_many_invalid_calls"] | None


class StrictToolCallRequest(ToolCallRequest):
    model_config = ConfigDict(extra="forbid")


VALIDATORS: dict[str, type[BaseModel]] = {
    "turns.request.json": TurnRequest,
    "turns.response.ok.json": TurnResponse,
    "turns.response.error.json": TurnResponse,
    "tool_calls.request.json": StrictToolCallRequest,
    "tool_calls.request.malformed.json": StrictToolCallRequest,
    "tool_calls.response.ok.json": ToolCallResponse,
    "tool_calls.response.end_turn.json": ToolCallResponse,
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_every_fixture_has_a_validator() -> None:
    assert {p.name for p in FIXTURES.glob("*.json")} == set(VALIDATORS)


@pytest.mark.parametrize("name", sorted(VALIDATORS))
def test_fixture_matches_contract_model(name: str) -> None:
    model = VALIDATORS[name]
    data = _load(name)

    model.model_validate(data)

    assert list(data) == list(model.model_fields)


def test_error_response_carries_a_non_empty_error() -> None:
    data = TurnResponse.model_validate(_load("turns.response.error.json"))

    assert data.stop_reason == "error"
    assert data.error


def test_ok_response_round_usage_is_reported_per_round() -> None:
    data = TurnResponse.model_validate(_load("turns.response.ok.json"))

    assert [r.usage.prompt_tokens for r in data.assistant_rounds] == [500, 312]
    assert sum(r.usage.completion_tokens for r in data.assistant_rounds) == data.usage.completion_tokens


def test_end_turn_response_names_an_end_turn_reason() -> None:
    assert ToolCallResponse.model_validate(_load("tool_calls.response.end_turn.json")).end_turn


def test_malformed_request_has_unparseable_arguments_in_a_valid_body() -> None:
    body = ToolCallRequest.model_validate(_load("tool_calls.request.malformed.json"))

    with pytest.raises(ValueError):
        json.loads(body.arguments)

"""Tool-call endpoint the harness calls during a turn (contract: section 6 of
docs/harness-smart-hospital-operations.md).

The harness owns the model loop; every tool call it makes lands here and runs
through the same `ToolTurn` the in-process agent uses, so validation,
grounding, caps and audit are identical in both runtimes.

Never published by a reverse proxy. A missing or wrong token answers exactly
like an unknown path, so the surface is not advertised; the token is checked
before the body is parsed so a malformed body cannot turn that 404 into a 422.
"""

import hmac
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app.agent import harness_registry
from app.config import get_settings

router = APIRouter(prefix="/internal/v1", tags=["internal"])


class ToolCallRequest(BaseModel):
    round: int = Field(ge=1, le=10_000)
    call_id: str
    name: str
    arguments: str = Field(max_length=1_000_000)


def _token_matches(request: Request) -> bool:
    expected = get_settings().harness_token
    supplied = request.headers.get("x-harness-token")
    if not expected or supplied is None:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


@router.post("/turns/{turn_id}/tool_calls")
async def tool_call(turn_id: str, request: Request) -> dict[str, str | None]:
    if not _token_matches(request):
        raise HTTPException(status_code=404)
    try:
        body = ToolCallRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid request body") from exc
    turn = harness_registry.get(turn_id)
    if turn is None:
        raise HTTPException(status_code=404)
    try:
        parsed = json.loads(body.arguments)
    except ValueError:
        parsed = None
    content = await turn.call(
        call_id=body.call_id,
        name=body.name,
        args=parsed if isinstance(parsed, dict) else None,
        round_num=body.round,
    )
    return {"content": content, "end_turn": turn.terminated_reason}

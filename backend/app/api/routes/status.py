"""Which of the agent's moving parts are on, for the UI's status lights.

Why it exists: three optional pieces decide how a request is answered -- the
agent runtime (in-process or harness), the harness process behind it, and the
Jev fast path -- and from the chat alone there is no way to tell whether one
is down. This reports each as `on`, `off` (not configured) or `down`
(configured but not answering).

Cheap by design: Jev is reported from configuration only, because a live
probe would be a paid call. The harness is probed with its own unauthenticated
`/healthz`, only when the harness runtime is selected. Nothing here exposes a
URL, model name or key.

What calls it: the frontend's status lights, on a timer.
"""

from typing import Literal

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from app.config import Settings, get_settings

router = APIRouter(prefix="/api/status", tags=["status"])

Light = Literal["on", "off", "down"]

# Short: this runs on a UI timer, and a harness that cannot answer a health
# check in a second is as good as down for a user waiting on a reply.
HEALTH_TIMEOUT_SECONDS = 1.0


class AgentStatus(BaseModel):
    runtime: Literal["in_process", "harness"]
    status: Light


class HarnessStatus(BaseModel):
    status: Light


class JevStatus(BaseModel):
    status: Literal["on", "off"]


class StatusResponse(BaseModel):
    agent: AgentStatus
    harness: HarnessStatus
    jev: JevStatus


async def _harness_is_up(settings: Settings) -> bool:
    """True when the gateway answers `ok` for the model the backend expects."""
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{settings.harness_url}/healthz")
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return (
        response.status_code == 200
        and isinstance(body, dict)
        and body.get("status") == "ok"
        and body.get("model") == settings.harness_model
    )


def _in_process_agent_has_key(settings: Settings) -> bool:
    key = settings.anthropic_api_key if settings.llm_provider == "anthropic" else settings.openrouter_api_key
    return bool(key)


@router.get("", response_model=StatusResponse)
async def status() -> StatusResponse:
    settings = get_settings()
    harness: Light = "off"
    if settings.agent_runtime == "harness":
        harness = "on" if await _harness_is_up(settings) else "down"
    agent: Light = harness if settings.agent_runtime == "harness" else (
        "on" if _in_process_agent_has_key(settings) else "off"
    )
    jev_on = settings.enable_jev_fast_path and bool(settings.typesafe_api_key)
    return StatusResponse(
        agent=AgentStatus(runtime=settings.agent_runtime, status=agent),
        harness=HarnessStatus(status=harness),
        jev=JevStatus(status="on" if jev_on else "off"),
    )

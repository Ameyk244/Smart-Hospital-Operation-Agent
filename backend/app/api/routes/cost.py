"""The Jev fast path's cost comparison — does this experiment actually pay?

Why it exists: `app/agent/jev_fast_path.py` is justified entirely on cost.
Its whole claim is "a Jev call is orders of magnitude cheaper than the
Sonnet agent turn it displaces". That claim should be *measurable from the
trace*, not asserted in a docstring — so this endpoint aggregates the
`jev_invoked` events the chat route already writes and turns them into the
two numbers that matter: what Jev cost, and what it avoided.

It is deliberately an honest-arithmetic endpoint, not a billing system.
Jev's side is measured (real `input_tokens` from the API response, recorded
per call). Sonnet's side is *estimated*: we cannot know what a turn that
never happened would have cost, so the avoided cost is computed from a
documented assumption, and every constant used is echoed back in the
response's `assumptions` block so the UI can show its work rather than
presenting a modelled number as a measurement.

What calls it: the frontend's cost-comparison table.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.chat import JEV_EVENT_TYPE
from app.db.models.agent import AgentEvent, EventStatus
from app.db.repositories.event_repository import EventRepository
from app.db.session import get_db

router = APIRouter(prefix="/api/cost-comparison", tags=["cost"])

# --- Sonnet pricing -------------------------------------------------------
# Published list price for `claude-sonnet-5`, which is this project's
# configured model (`Settings.anthropic_model` in app/config.py). USD per
# 1M tokens.
SONNET_INPUT_USD_PER_MTOK = 2.00
SONNET_OUTPUT_USD_PER_MTOK = 10.00

# --- Measured fixed overhead of one agent turn ----------------------------
# Measured against this codebase, not guessed: every agent turn pays these
# input tokens before the user's message is counted at all.
#   SYSTEM_PROMPT in app/agent/graph.py: 1,757 chars ~= 440 tokens
SONNET_SYSTEM_PROMPT_TOKENS = 440
#   All 7 tool schemas bound to the model (app/agent/tools/*): 5,580 chars
#   ~= 1,395 tokens
SONNET_TOOL_SCHEMA_TOKENS = 1_395
#   The floor: what a turn costs on input before any conversation content.
SONNET_FIXED_INPUT_TOKENS = SONNET_SYSTEM_PROMPT_TOKENS + SONNET_TOOL_SCHEMA_TOKENS

# --- Assumption: the shape of one avoided turn ----------------------------
# ESTIMATE, not a measurement — this is the whole point of echoing the
# assumptions back. A turn the fast path prevented was never executed, so its
# true token count is unknowable. We model it as the measured fixed overhead
# above plus a short user message, and a short reply on output. Named
# constants so a reader can see exactly which number is modelled and change
# it without hunting through arithmetic.
ASSUMED_AVOIDED_TURN_INPUT_TOKENS = 1_900
ASSUMED_AVOIDED_TURN_OUTPUT_TOKENS = 120

# --- Jev pricing ----------------------------------------------------------
# $42 per billion input tokens == $0.042 per 1M. Output tokens are free.
JEV_INPUT_USD_PER_MTOK = 0.042
JEV_OUTPUT_USD_PER_MTOK = 0.0

# Fallback used only when a completed Jev call recorded a null
# `input_tokens` (the SDK types usage as `int | None`). Deliberately NOT
# applied to failed calls: a call that errored or timed out is not assumed
# to have consumed tokens.
#
# No longer a guess. This was originally estimated at 400 from reading the
# criteria payload; the first real call against jev-1.13.0 reported 1,008
# input tokens for a short message, so the estimate was ~2.5x low — the four
# Choice questions and all their label descriptions cost more than they look
# like they should. Rounded to 1,000 from that single measurement, which is
# one data point, not a distribution.
ESTIMATED_JEV_INPUT_TOKENS_PER_CALL = 1_000


class CostComparisonAssumptions(BaseModel):
    sonnet_model: str = "claude-sonnet-5"
    sonnet_input_usd_per_mtok: float = SONNET_INPUT_USD_PER_MTOK
    sonnet_output_usd_per_mtok: float = SONNET_OUTPUT_USD_PER_MTOK
    sonnet_system_prompt_tokens: int = SONNET_SYSTEM_PROMPT_TOKENS
    sonnet_tool_schema_tokens: int = SONNET_TOOL_SCHEMA_TOKENS
    sonnet_fixed_input_tokens: int = SONNET_FIXED_INPUT_TOKENS
    assumed_avoided_turn_input_tokens: int = ASSUMED_AVOIDED_TURN_INPUT_TOKENS
    assumed_avoided_turn_output_tokens: int = ASSUMED_AVOIDED_TURN_OUTPUT_TOKENS
    jev_input_usd_per_mtok: float = JEV_INPUT_USD_PER_MTOK
    jev_output_usd_per_mtok: float = JEV_OUTPUT_USD_PER_MTOK
    estimated_jev_input_tokens_per_call: int = ESTIMATED_JEV_INPUT_TOKENS_PER_CALL
    note: str = (
        "Jev token counts are measured per call from the API response. "
        "Avoided Sonnet cost is an estimate: the displaced agent turns never "
        "ran, so their true token usage is unknowable. The fixed-overhead "
        "figures were measured from this codebase's system prompt and bound "
        "tool schemas."
    )


class CostComparison(BaseModel):
    session_id: str | None = None
    jev_calls_total: int
    jev_fast_path_hits: int
    jev_fallthroughs: int
    jev_failures: int
    jev_input_tokens: int
    jev_output_tokens: int
    jev_cost_usd: float
    avoided_agent_turns: int
    avoided_sonnet_cost_usd: float
    net_savings_usd: float
    assumptions: CostComparisonAssumptions = CostComparisonAssumptions()


def _status_value(event: AgentEvent) -> str:
    """`AgentEvent.status` is a `str` enum persisted into a plain `String(20)`
    column, so it reads back as a bare string on a fresh load but as an
    `EventStatus` on an identity-mapped instance. Normalize either shape."""
    return getattr(event.status, "value", event.status)


def _recorded_tokens(event: AgentEvent, key: str) -> int | None:
    args: Any = event.arguments_json
    if not isinstance(args, dict):
        return None
    value = args.get(key)
    return value if isinstance(value, int) else None


@router.get("", response_model=CostComparison)
async def cost_comparison(
    session_id: str | None = None, db: AsyncSession = Depends(get_db)
) -> CostComparison:
    """Aggregate `jev_invoked` events into a Jev-vs-Sonnet cost comparison.

    With no `session_id`, aggregates across every session; with one, scopes
    to that session. A session with no Jev events is not an error — it
    returns an all-zero comparison, which is the honest answer.
    """
    events = await EventRepository(db).list_by_event_type(
        JEV_EVENT_TYPE, session_id=session_id
    )

    hits = 0
    failures = 0
    jev_input_tokens = 0
    jev_output_tokens = 0

    for event in events:
        status = _status_value(event)
        if status == EventStatus.SUCCESS.value:
            hits += 1
        elif status == EventStatus.FAILURE.value:
            # Errored/timed-out/never-ran: counted as a call attempt, but no
            # token cost is imputed to it. See the constant's comment.
            failures += 1
            continue

        recorded_in = _recorded_tokens(event, "input_tokens")
        jev_input_tokens += (
            recorded_in if recorded_in is not None else ESTIMATED_JEV_INPUT_TOKENS_PER_CALL
        )
        jev_output_tokens += _recorded_tokens(event, "output_tokens") or 0

    total = len(events)
    jev_cost = (
        jev_input_tokens / 1_000_000 * JEV_INPUT_USD_PER_MTOK
        + jev_output_tokens / 1_000_000 * JEV_OUTPUT_USD_PER_MTOK
    )
    # One hit == one agent turn that did not happen.
    avoided_sonnet_cost = hits * (
        ASSUMED_AVOIDED_TURN_INPUT_TOKENS / 1_000_000 * SONNET_INPUT_USD_PER_MTOK
        + ASSUMED_AVOIDED_TURN_OUTPUT_TOKENS / 1_000_000 * SONNET_OUTPUT_USD_PER_MTOK
    )

    return CostComparison(
        session_id=session_id,
        jev_calls_total=total,
        jev_fast_path_hits=hits,
        # Every consultation that did not displace an agent turn — whether
        # Jev declined or the call failed.
        jev_fallthroughs=total - hits,
        jev_failures=failures,
        jev_input_tokens=jev_input_tokens,
        jev_output_tokens=jev_output_tokens,
        # 9dp, not 2: one Jev call costs on the order of $0.00002, which
        # rounds to zero at anything coarser — making the comparison read as
        # "cost nothing" rather than "cost very little", and quietly
        # inflating net savings. The UI is responsible for display rounding.
        jev_cost_usd=round(jev_cost, 9),
        avoided_agent_turns=hits,
        avoided_sonnet_cost_usd=round(avoided_sonnet_cost, 9),
        net_savings_usd=round(avoided_sonnet_cost - jev_cost, 9),
    )

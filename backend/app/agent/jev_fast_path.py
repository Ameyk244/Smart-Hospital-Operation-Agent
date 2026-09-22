"""The Jev fast path — widening the deterministic entrance, not replacing a gate.

Why it exists: `app/parser/parser.py` is a deliberately tiny, fixed regex
grammar (five rules). That is a feature, not a limitation — it is what makes
the deterministic path *deterministic*. But it also means a request one word
off the grammar ("what departments do you have?" vs. the literal "list
departments") falls all the way through to a full Claude Sonnet agent turn:
~1,800 input tokens of system prompt and tool schemas before the user's
message is even counted, several hundred milliseconds of latency, and real
API spend — to run the exact same read-only command the regex would have run
for free.

This module asks a cheaper, narrower question in between: *does this
unmatched message map to one of our handful of known deterministic commands?*
It asks TypeSafe AI's Jev ("System One"), which returns a typed, calibrated
decision (a label plus a probability distribution) rather than free text. A
confident match is handed to the **existing** `CommandRunner` exactly as if
the regex had matched — same trusted execution chokepoint, same handlers,
same results. Anything else falls through to the agent exactly as before.

What this is NOT:
  - It is **not** a replacement for the domain gate
    (`app/agent/domain_gate.py`). A prior audit concluded Jev is a poor fit
    there: the domain gate is free, synchronous, and conservative-by-design,
    and paying an API call to reject off-topic requests would defeat the
    point of rejecting them cheaply. The fast path therefore runs *after*
    the domain gate and the eligibility gate, never before — see
    `app/api/routes/chat.py` for the enforced ordering.
  - It is **not** a general NLU layer bolted onto the parser. It can only
    ever emit a `Command` from a closed, hardcoded set that `CommandRunner`
    already knows how to execute. It cannot invent a command, and (see the
    argument strategy below) it cannot invent an argument value either.
  - It is **not** a new execution path. It constructs a `Command` and hands
    it off; every hospital operation still happens in exactly one place.

The command set, and why `search_patients` is excluded
------------------------------------------------------
The `criteria` dict below is derived from what the parser grammar can
actually produce *and* what `CommandRunner` accepts — not invented:

  - `list_departments`            (no arguments)
  - `list_scanners`               (optional `type`, `status`)
  - `show_next_appointment`       (no arguments in the parser's usage)
  - `list_delayed_appointments`   (optional `appointment_type`)
  - `search_patients`             — RECOGNIZED BUT NEVER EXECUTED
  - `none`                        — an explicit no-match option so unsupported
                                    requests are never forced onto a command

`search_patients` needs a free-text `query` (a patient's name). A `Choice`
question fundamentally cannot produce free text — it returns one label from
a fixed list — and the one thing this module must never do is fabricate an
argument. So `search_patients` is **excluded from the fast path entirely**.
It stays in `criteria` only as a *recognition* label: giving Jev somewhere
correct to put "find patient Maria Alvarez" is what stops that message being
forced onto a neighbouring label like `show_next_appointment`. When it is
chosen, this module reports a non-match (`failure_reason="unsupported_command"`)
and the request falls through to the agent, which has a real search tool and
can read the name out of the text.

Argument strategy: ask in the same round trip, and omit rather than invent
--------------------------------------------------------------------------
Two of the executable commands take optional filters. Rather than a second
round trip (which would double the latency this feature exists to save),
all questions go in a **single** `system_one` call: the command choice plus
one `Choice` per supported filter. Irrelevant answers are simply ignored —
we only read the filter answers belonging to the command that was chosen.

Every filter question carries an explicit `"unspecified"` option, and a
filter is only applied when its answer clears the same confidence threshold
*and* is not `"unspecified"`. Otherwise the argument is **omitted**, never
guessed. Omission is safe by construction: `list_scanners` with no filters
lists all scanners and `list_delayed_appointments` with no type lists all
delayed appointments — a strictly wider, still-correct read-only result,
which is the right failure mode for an optimization. A fabricated filter
("they must have meant MRI") would silently return the *wrong* answer, which
is not.

Failure handling: this must never be able to break a chat turn
---------------------------------------------------------------
Every outcome of this function is a `JevFastPathResult`. It does not raise.
`typesafe_sdk` is imported lazily, inside the call, so an unexpectedly missing
or broken installation is an ordinary non-match with
`failure_reason="jev_sdk_missing"` rather than an import-time explosion. API
errors, timeouts and a missing API key are handled the same way: return
`matched=False`, record why, fall through to the agent.
A Jev outage degrades this feature back to today's behavior and nothing else.

What calls it: `app/api/routes/chat.py`, for requests that the parser did
not match and that have already passed both the domain gate and the
eligibility gate, immediately before the agent would otherwise be invoked.
Guarded there on `settings.enable_jev_fast_path`, so with the flag off no
client is ever constructed and this module is never even imported at
request time.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.execution.commands import Command
from app.observability.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from app.config import Settings

_logger = get_logger("agent.jev_fast_path")

# The label Jev returns when the message maps to none of our commands.
# An explicit "none" option gives the model somewhere honest to put a
# non-match instead of forcing it onto the least-bad executable label.
NONE_CHOICE = "none"

# The question keys of the single `system_one` call. Kept as constants
# because both the request construction and the answer reading refer to them.
COMMAND_QUESTION = "command"
SCANNER_TYPE_QUESTION = "scanner_type"
SCANNER_STATUS_QUESTION = "scanner_status"
APPOINTMENT_TYPE_QUESTION = "appointment_type"

# The answer label meaning "the message did not specify this filter". See the
# module docstring: an unspecified (or unconfident) filter is omitted from
# the Command, never guessed at.
UNSPECIFIED = "unspecified"

# Commands this module may construct and hand to CommandRunner. Every name
# here is registered in `app/execution/commands/*` and reachable from the
# parser's own grammar. `search_patients` is deliberately absent — see the
# module docstring.
EXECUTABLE_COMMANDS: frozenset[str] = frozenset(
    {
        "list_departments",
        "list_scanners",
        "show_next_appointment",
        "list_delayed_appointments",
    }
)

# Recognized so Jev has a correct place to put patient-name searches, but
# never executed by this path.
RECOGNIZED_BUT_UNSUPPORTED: frozenset[str] = frozenset({"search_patients"})

_COMMAND_INSTRUCTIONS = (
    "You are routing a message sent to a hospital operations assistant. "
    "Decide which single known command, if any, the message is asking to "
    "run. Choose 'none' unless the message clearly and unambiguously maps "
    "to exactly one command; 'none' is the correct answer for anything "
    "conversational, ambiguous, multi-step, or asking to change data."
)

# Descriptions are written from the *user's* point of view (what someone
# would be asking for), not the implementation's, since that is what Jev is
# matching the message text against.
_COMMAND_CRITERIA: dict[str, str] = {
    "list_departments": (
        "The message asks what departments the hospital has, or to list or "
        "show the departments. No other filter or detail is requested."
    ),
    "list_scanners": (
        "The message asks what scanners or imaging machines exist, or to "
        "list/show them, optionally narrowed by modality (MRI, CT, X-ray) "
        "or by status (available, in use, under maintenance)."
    ),
    "show_next_appointment": (
        "The message asks what the next or upcoming appointment is, for no "
        "particular named patient."
    ),
    "list_delayed_appointments": (
        "The message asks which appointments are delayed or running late, "
        "or how many are delayed, optionally narrowed by modality "
        "(MRI, CT, X-ray)."
    ),
    "search_patients": (
        "The message asks to look up, find or show a patient by name."
    ),
    NONE_CHOICE: (
        "The message does not map cleanly to any single command above. This "
        "includes greetings and chit-chat, questions about preferences, "
        "anything asking to reschedule, move, book or change something, "
        "multi-step requests, and anything ambiguous between two commands."
    ),
}

_MODALITY_CRITERIA: dict[str, str] = {
    "MRI": "The message explicitly mentions MRI.",
    "CT": "The message explicitly mentions CT or a CT scan.",
    "XRAY": "The message explicitly mentions X-ray or xray.",
    UNSPECIFIED: (
        "The message does not explicitly name a single imaging modality, or "
        "names more than one."
    ),
}

_SCANNER_STATUS_CRITERIA: dict[str, str] = {
    "AVAILABLE": "The message asks specifically for available or free scanners.",
    "IN_USE": "The message asks specifically for scanners that are in use or busy.",
    "MAINTENANCE": "The message asks specifically for scanners under maintenance.",
    UNSPECIFIED: (
        "The message does not restrict scanners to a single status, or names "
        "more than one."
    ),
}

# Which filter question (if any) feeds which Command argument, per command.
# Anything not listed here takes no arguments from this path.
_COMMAND_ARGUMENTS: dict[str, dict[str, str]] = {
    "list_scanners": {
        "type": SCANNER_TYPE_QUESTION,
        "status": SCANNER_STATUS_QUESTION,
    },
    "list_delayed_appointments": {
        "appointment_type": APPOINTMENT_TYPE_QUESTION,
    },
}


@dataclass(frozen=True)
class JevFastPathResult:
    """Everything the caller needs to route the turn *and* to write the
    `jev_invoked` trace event, in one immutable value.

    `matched=True` is the only state in which `command` is populated, and it
    is always a `Command` whose name `CommandRunner` has a handler for.
    `failure_reason` is set on every non-match — including the benign ones
    (unconfident, chose `none`) — so the trace can distinguish "Jev ran and
    declined" from "Jev never ran".
    """

    matched: bool
    command: Command | None = None
    choice: str | None = None
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    failure_reason: str | None = None

    @property
    def is_failure(self) -> bool:
        """True when Jev never produced a usable answer (error/timeout/missing
        SDK or key), as opposed to answering "no match". The caller maps this
        onto `EventStatus.FAILURE` vs `EventStatus.REJECTED`."""
        return self.failure_reason in _FAILURE_REASONS


# Failure reasons that mean "the call did not complete", distinct from the
# reasons that mean "the call completed and the answer was not actionable".
_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "jev_sdk_missing",
        "jev_missing_api_key",
        "jev_timeout",
        "jev_api_error",
        "jev_unexpected_error",
        # A response whose shape we can't read counts as a failure even
        # though the HTTP call completed: there is no decision in it to
        # report. This matters most on first contact with a real key — if
        # the SDK's actual response shape differs from the documented one,
        # *every* call lands here, and classifying it as REJECTED would
        # drop the error_category and make the trace read "Jev wasn't
        # confident" for what is really a total integration failure.
        "jev_malformed_response",
    }
)


def _build_questions(choice_cls: Any) -> dict[str, Any]:
    """The single multi-question payload. Built here (not at import time)
    because `Choice` comes from the lazily imported SDK."""
    return {
        COMMAND_QUESTION: choice_cls(
            instructions=_COMMAND_INSTRUCTIONS,
            criteria=dict(_COMMAND_CRITERIA),
        ),
        SCANNER_TYPE_QUESTION: choice_cls(
            instructions=(
                "If the message is asking about scanners, which single "
                "imaging modality does it restrict them to?"
            ),
            criteria=dict(_MODALITY_CRITERIA),
        ),
        SCANNER_STATUS_QUESTION: choice_cls(
            instructions=(
                "If the message is asking about scanners, which single "
                "scanner status does it restrict them to?"
            ),
            criteria=dict(_SCANNER_STATUS_CRITERIA),
        ),
        APPOINTMENT_TYPE_QUESTION: choice_cls(
            instructions=(
                "If the message is asking about appointments, which single "
                "imaging modality does it restrict them to?"
            ),
            criteria=dict(_MODALITY_CRITERIA),
        ),
    }


def _answer_label(response: Any, question: str, threshold: float) -> str | None:
    """A filter answer, but only if it is confident and actually specified.
    Returns None otherwise, which the caller turns into an *omitted*
    argument — never a guessed one. Defensive about the answer's shape:
    a filter we can't read is simply a filter we don't apply."""
    try:
        answer = response.answers[question]
        label = answer.choice
        confidence = float(answer.confidence)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if label == UNSPECIFIED or confidence < threshold:
        return None
    return str(label)


def _build_command(choice: str, response: Any, threshold: float) -> Command | None:
    """Turns a confident command label into a `Command` CommandRunner accepts,
    or None if the label isn't one this path may execute."""
    if choice not in EXECUTABLE_COMMANDS:
        return None
    args: dict[str, Any] = {}
    for arg_name, question in _COMMAND_ARGUMENTS.get(choice, {}).items():
        label = _answer_label(response, question, threshold)
        if label is not None:
            args[arg_name] = label
    return Command(choice, args)


def _coerce_probabilities(raw: Any) -> dict[str, float] | None:
    """`probabilities` is written straight into `AgentEvent.arguments_json`
    (a JSON column), so it has to be plain JSON-safe types, not whatever
    mapping the SDK happens to return."""
    if not isinstance(raw, dict):
        return None
    try:
        return {str(k): float(v) for k, v in raw.items()}
    except (TypeError, ValueError):
        return None


async def try_jev_fast_path(text: str, settings: "Settings") -> JevFastPathResult:
    """Ask Jev whether `text` maps to a known deterministic command.

    Never raises. Returns `matched=True` only when all three hold:
      1. the command answer's confidence >= `settings.jev_confidence_threshold`,
      2. the chosen label is not `"none"`, and
      3. the label maps to a `Command` `CommandRunner` can actually execute
         (which excludes `search_patients` — see the module docstring).

    The caller is responsible for the trace event and for falling through to
    the agent on any non-match; this function has no side effects beyond the
    outbound API call.
    """
    if not settings.enable_jev_fast_path:
        # Belt-and-braces: the caller already guards on this flag so that no
        # client is constructed when the feature is off. Repeated here so the
        # function is safe to call directly (e.g. from a test) regardless.
        return JevFastPathResult(matched=False, failure_reason="disabled")

    if not settings.typesafe_api_key:
        # Checked before constructing a client, so a missing key can never
        # turn into an outbound request that fails slowly (or, worse, an
        # accidental live call from a test environment).
        return JevFastPathResult(matched=False, failure_reason="jev_missing_api_key")

    try:
        from typesafe_sdk import Choice, TypeSafeClient, TypeSafeError
    except ImportError:
        # Its absence must degrade like any other Jev integration failure
        # rather than breaking chat.
        return JevFastPathResult(matched=False, failure_reason="jev_sdk_missing")

    threshold = settings.jev_confidence_threshold

    def _call() -> Any:
        # The key is passed explicitly rather than letting the SDK read
        # TYPESAFE_API_KEY from the environment itself. That default would
        # silently never work here: this project loads .env through
        # pydantic-settings into `Settings`, which does *not* export anything
        # into os.environ — so a bare TypeSafeClient() finds no key and fails
        # instantly with an auth error, even though the key is sitting right
        # there in settings. Caught on the first live call; see the
        # `jev_missing_api_key` guard above for the no-key-at-all case.
        questions = _build_questions(Choice)
        with TypeSafeClient(api_key=settings.typesafe_api_key) as client:
            try:
                return client.system_one(
                    state=text, questions=questions, model=settings.jev_model
                )
            except TypeError:
                # Defensive: SDK 0.7.1 does accept a `model` kwarg on
                # `system_one` (verified by introspection), but if a future
                # version drops it, fall back to the SDK's own default rather
                # than hard-failing every call; `response.model` records what
                # actually ran.
                return client.system_one(state=text, questions=questions)

    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_call), timeout=settings.jev_timeout_seconds
        )
    except (TimeoutError, asyncio.TimeoutError):
        # NOTE: wait_for cannot cancel the worker thread itself — the
        # underlying HTTP request may still be in flight. That is acceptable
        # here: we only need to stop *waiting* so the turn can proceed to the
        # agent. The orphaned thread ends on its own.
        return JevFastPathResult(
            matched=False,
            latency_ms=_elapsed_ms(started),
            failure_reason="jev_timeout",
        )
    except TypeSafeError as exc:
        # The documented base class: every TypeSafe API/connection/validation
        # error subclasses it, so this one clause covers auth, rate limits,
        # 5xx, connection failures and response-validation failures alike.
        _logger.warning("jev_api_error", error=type(exc).__name__)
        return JevFastPathResult(
            matched=False,
            latency_ms=_elapsed_ms(started),
            failure_reason="jev_api_error",
        )
    except Exception as exc:  # noqa: BLE001 - see comment
        # Deliberately broad, and deliberately only here. This module sits on
        # the hot request path purely as a cost optimization; an unforeseen
        # exception from a third-party SDK must degrade to "fall through to
        # the agent", never surface as a failed chat turn.
        _logger.warning("jev_unexpected_error", error=type(exc).__name__)
        return JevFastPathResult(
            matched=False,
            latency_ms=_elapsed_ms(started),
            failure_reason="jev_unexpected_error",
        )

    latency_ms = _elapsed_ms(started)
    usage_in, usage_out, model = _read_usage(response, settings)

    try:
        command_answer = response.answers[COMMAND_QUESTION]
        choice = str(command_answer.choice)
        confidence = float(command_answer.confidence)
        probabilities = _coerce_probabilities(
            getattr(command_answer, "probabilities", None)
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return JevFastPathResult(
            matched=False,
            latency_ms=latency_ms,
            input_tokens=usage_in,
            output_tokens=usage_out,
            model=model,
            failure_reason="jev_malformed_response",
        )

    base = {
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities,
        "latency_ms": latency_ms,
        "input_tokens": usage_in,
        "output_tokens": usage_out,
        "model": model,
    }

    if confidence < threshold:
        return JevFastPathResult(matched=False, failure_reason="low_confidence", **base)
    if choice == NONE_CHOICE:
        return JevFastPathResult(matched=False, failure_reason="no_command_match", **base)

    command = _build_command(choice, response, threshold)
    if command is None:
        # Either the recognized-but-unsupported `search_patients`, or a label
        # we don't have a handler for. Both fall through to the agent.
        reason = (
            "unsupported_command"
            if choice in RECOGNIZED_BUT_UNSUPPORTED
            else "unknown_command"
        )
        return JevFastPathResult(matched=False, failure_reason=reason, **base)

    return JevFastPathResult(matched=True, command=command, **base)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _read_usage(response: Any, settings: "Settings") -> tuple[int | None, int | None, str | None]:
    """Token usage and the model that actually ran. All three are optional in
    the SDK contract (`usage.input_tokens` is `int | None`), and the cost
    endpoint handles nulls explicitly, so this never invents a number."""
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None) if usage is not None else None
    output_tokens = getattr(usage, "output_tokens", None) if usage is not None else None
    model = getattr(response, "model", None) or settings.jev_model
    return (
        int(input_tokens) if isinstance(input_tokens, int) else None,
        int(output_tokens) if isinstance(output_tokens, int) else None,
        str(model) if model is not None else None,
    )

"""PHI and secret redaction for everything this app writes as telemetry.

Why it exists: the agent trace stores each tool call's arguments in
`agent_events.arguments_json`, and the trace panel shows them verbatim. Some
arguments are free text a user typed or the model wrote, e.g.
`execute_command(command_text="show patient John Smith")`, a preference
value, or the raw args of a rejected call. Storing those turns an
operational trace into a copy of patient data. Separately, uvicorn's access
log prints full request paths, including
`/api/operations/patients?query=<a patient's name>`.

Design: **fail closed**. Nothing is passed through because it "looks
harmless". Each argument key that may be stored is allowlisted together with
the shape its value must have: an entity code, an upper-case enum, an ISO
datetime, a number, or a lower-case identifier. A value that isn't allowed
is replaced with `REDACTED`.

- An allowlisted key whose value has the wrong shape is redacted too. A
  patient name that the model put into `patient_code` doesn't slip through
  on the strength of the key name.
- Keys are kept. A rejected call still shows *that* `command_text` was
  passed, which is what debugging needs, and consumers that check the key
  set (the Jev trace row) are unaffected.
- A key that isn't itself a plain identifier is dropped, and counted under
  `redacted_keys`.

Entity codes (`PT-1001`, `APT-2001`, ...) are deliberately kept. They are
pseudonymous internal keys, already shown to the same session by the
grounding ledger, and a trace without them can't be followed. Patient names,
MRNs and free text are what this removes.

What calls it: `app/observability/tracing.py` (every `agent_events` write),
`app/observability/logging_config.py` (a structlog processor over every log
line, plus the uvicorn access-log filter).
"""

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

REDACTED = "[REDACTED]"

_CODE = re.compile(r"^[A-Z]{2,5}-[A-Z0-9]{1,10}$")  # PT-1001, APT-2001, SCN-1, DEPT-RAD
_ENUM = re.compile(r"^[A-Z][A-Z_]{0,31}$")  # MRI, DELAYED, IN_USE
_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")  # list_scanners, none
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")  # jev-latest
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _matches(pattern: re.Pattern[str]) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, str) and bool(pattern.match(value))


def _is_probability_map(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and _IDENTIFIER.match(k) and _is_number(v) for k, v in value.items()
    )


# key -> predicate the value must satisfy (None is always allowed). Adding a
# key here is a deliberate statement that its values can never carry PHI.
_ALLOWED_ARGUMENTS: dict[str, Callable[[Any], bool]] = {
    # entity codes (search/observation/reschedule tool args)
    "appointment_code": _matches(_CODE),
    "scanner_code": _matches(_CODE),
    "patient_code": _matches(_CODE),
    "department_code": _matches(_CODE),
    # enums
    "status": _matches(_ENUM),
    "appointment_type": _matches(_ENUM),
    "type": _matches(_ENUM),
    # reschedule target time
    "new_start": _matches(_DATETIME),
    # counts / measurements
    "limit": _is_number,
    "tool_call_count": _is_number,
    "buffered_ms": _is_number,
    # Jev consultation (`jev_invoked`): all seven keys the trace panel reads
    "choice": _matches(_IDENTIFIER),
    "confidence": _is_number,
    "probabilities": _is_probability_map,
    "threshold": _is_number,
    "input_tokens": _is_number,
    "output_tokens": _is_number,
    "model": _matches(_MODEL_ID),
}


def redact_arguments(arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """The fail-closed allowlist applied to every `agent_events` row's
    `arguments` before it is stored."""
    if arguments is None:
        return None
    if not isinstance(arguments, dict):
        return {"value": REDACTED}

    redacted: dict[str, Any] = {}
    dropped_keys = 0
    for key, value in arguments.items():
        if not isinstance(key, str) or not _KEY.match(key):
            dropped_keys += 1
            continue
        allowed = _ALLOWED_ARGUMENTS.get(key)
        if value is None or (allowed is not None and allowed(value)):
            redacted[key] = value
        else:
            redacted[key] = REDACTED
    if dropped_keys:
        redacted["redacted_keys"] = dropped_keys
    return redacted


def redact_tool_name(tool_name: str | None) -> str | None:
    """`tool_name` is normally one of ours, but on an `unknown_tool`
    rejection it is whatever name the model invented. Only an identifier is
    kept."""
    if tool_name is None or _IDENTIFIER.match(tool_name):
        return tool_name
    return REDACTED


# --------------------------------------------------------------------------
# Log lines


# Values under these keys are free text or credentials, so they are never
# logged even when a future call site passes them by accident.
_SENSITIVE_LOG_KEYS = frozenset(
    {
        "arguments",
        "authorization",
        "command_text",
        "password",
        "query",
        "text",
        "transcript",
        "user_text",
        "value",
    }
)
_SENSITIVE_LOG_SUFFIXES = ("_api_key", "_password", "_secret")


def secret_values_from_settings(settings: Any) -> list[str]:
    """Every configured credential: the API keys and the database
    password. These are scrubbed from any log line they appear in."""
    values = [
        getattr(settings, "anthropic_api_key", None),
        getattr(settings, "openrouter_api_key", None),
        getattr(settings, "typesafe_api_key", None),
    ]
    database_url = getattr(settings, "database_url", None)
    if database_url:
        try:
            values.append(urlsplit(database_url).password)
        except ValueError:
            pass
    # Very short values would match ordinary text and scrub half of every line.
    return [v for v in values if isinstance(v, str) and len(v) >= 6]


def _scrub(value: Any, secrets: Iterable[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, secrets) for v in value)
    return value


def make_log_redactor(secret_values: Iterable[str]) -> Callable[..., dict[str, Any]]:
    """A structlog processor that redacts sensitive keys and scrubs secret
    values from every field. It runs after exception formatting, so a
    credential inside a traceback is caught too."""
    secrets = tuple(secret_values)

    def redact_log_event(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        for key in list(event_dict):
            lowered = key.lower()
            if lowered in _SENSITIVE_LOG_KEYS or lowered.endswith(_SENSITIVE_LOG_SUFFIXES):
                event_dict[key] = REDACTED
            elif secrets:
                event_dict[key] = _scrub(event_dict[key], secrets)
        return event_dict

    return redact_log_event


class AccessLogQueryStringFilter(logging.Filter):
    """Drops the query string from uvicorn access-log lines. Query strings
    carry search text (`?query=<patient name>`); the path and status code
    are what an access log is for."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn's access record: (client_addr, method, full_path, http_version, status_code)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path, sep, _query = args[2].partition("?")
            if sep:
                record.args = (*args[:2], f"{path}?{REDACTED}", *args[3:])
        return True

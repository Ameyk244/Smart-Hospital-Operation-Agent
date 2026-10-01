"""Redaction (observability Phase 1): the fail-closed allowlist on stored
trace arguments, the log-line processor, and the uvicorn access-log filter.
The end-to-end check, where a real agent turn involves a patient name, is
`tests/integration/test_trace_redaction.py`."""

import json
import logging

import structlog

from app.config import Settings
from app.observability import logging_config
from app.observability.redaction import (
    REDACTED,
    AccessLogQueryStringFilter,
    make_log_redactor,
    redact_arguments,
    redact_tool_name,
    secret_values_from_settings,
)


def test_free_text_command_is_redacted_but_key_is_kept():
    assert redact_arguments({"command_text": "show patient David Davis"}) == {
        "command_text": REDACTED
    }


def test_structured_search_arguments_pass_through():
    args = {
        "appointment_type": "MRI",
        "status": "DELAYED",
        "patient_code": "PT-1001",
        "department_code": "DEPT-RAD",
        "scanner_code": "SCN-2",
        "limit": 25,
    }
    assert redact_arguments(args) == args


def test_reschedule_arguments_pass_through():
    args = {"appointment_code": "APT-2001", "scanner_code": "SCN-2", "new_start": "2026-10-02T09:30:00"}
    assert redact_arguments(args) == args


def test_allowlisted_key_with_a_name_in_it_is_still_redacted():
    """The model put a patient's name where a code belongs. The key alone
    doesn't vouch for the value."""
    assert redact_arguments({"patient_code": "David Davis", "status": "anything goes"}) == {
        "patient_code": REDACTED,
        "status": REDACTED,
    }


def test_memory_preference_text_is_redacted():
    assert redact_arguments({"key": "note", "value": "call Mrs Smith's daughter"}) == {
        "key": REDACTED,
        "value": REDACTED,
    }


def test_nested_and_unknown_shapes_are_redacted():
    out = redact_arguments({"filters": {"name": "David Davis"}, "names": ["David Davis"]})
    assert out == {"filters": REDACTED, "names": REDACTED}


def test_numbers_under_unknown_keys_are_redacted():
    """A number can be an identifier too (a phone number, an MRN)."""
    assert redact_arguments({"phone": 5551234567}) == {"phone": REDACTED}


def test_non_identifier_keys_are_dropped_and_counted():
    out = redact_arguments({"David Davis": "x", "limit": 5})
    assert out == {"limit": 5, "redacted_keys": 1}
    assert "David Davis" not in json.dumps(out)


def test_jev_arguments_keep_all_seven_keys():
    args = {
        "choice": "list_scanners",
        "confidence": 0.95,
        "probabilities": {"list_scanners": 0.95, "none": 0.05},
        "threshold": 0.9,
        "input_tokens": 350,
        "output_tokens": 12,
        "model": "jev-latest",
    }
    assert redact_arguments(args) == args
    assert redact_arguments({**args, "choice": None}) == {**args, "choice": None}


def test_none_and_non_dict_arguments():
    assert redact_arguments(None) is None
    assert redact_arguments(["David Davis"]) == {"value": REDACTED}  # type: ignore[arg-type]


def test_tool_name_from_the_model_is_only_kept_as_an_identifier():
    assert redact_tool_name("search_appointments") == "search_appointments"
    assert redact_tool_name("lookup David Davis") == REDACTED
    assert redact_tool_name(None) is None


# --------------------------------------------------------------------------
# Log lines


def test_secret_values_include_api_keys_and_db_password():
    settings = Settings(
        database_url="postgresql+asyncpg://u:db-pass-xyz@localhost:5433/db",
        anthropic_api_key="sk-ant-secret-abc",
        typesafe_api_key="ts-secret-def",
    )
    assert set(secret_values_from_settings(settings)) == {
        "db-pass-xyz",
        "sk-ant-secret-abc",
        "ts-secret-def",
    }


def test_log_redactor_scrubs_secret_values_anywhere():
    redact = make_log_redactor(["sk-ant-secret-abc"])
    out = redact(
        None,
        "info",
        {
            "event": "x",
            "exception": "Traceback ... Authorization: Bearer sk-ant-secret-abc",
            "nested": {"h": ["sk-ant-secret-abc"]},
        },
    )
    assert "sk-ant-secret-abc" not in json.dumps(out)
    assert out["exception"].endswith(f"Bearer {REDACTED}")


def test_log_redactor_drops_sensitive_keys_but_not_token_counts():
    redact = make_log_redactor([])
    out = redact(
        None,
        "info",
        {
            "event": "x",
            "user_text": "show patient David Davis",
            "query": "Davis",
            "anthropic_api_key": "k",
            "input_tokens": 350,
        },
    )
    assert out == {
        "event": "x",
        "user_text": REDACTED,
        "query": REDACTED,
        "anthropic_api_key": REDACTED,
        "input_tokens": 350,
    }


def test_configured_logging_never_prints_an_api_key(capsys, monkeypatch):
    """End to end through the real structlog configuration: the key is
    passed as a field, interpolated into a message, and carried inside a
    traceback, and none of those reach stdout."""
    key = "sk-ant-live-should-never-print-1234"
    monkeypatch.setattr(
        logging_config,
        "get_settings",
        lambda: Settings(anthropic_api_key=key, typesafe_api_key="ts-also-secret-5678"),
    )
    logging_config.configure_logging()
    try:
        log = structlog.get_logger("test.redaction")
        log.info("calling provider", detail=f"x-api-key: {key}", anthropic_api_key=key)
        try:
            raise RuntimeError(f"401 for key {key} / ts-also-secret-5678")
        except RuntimeError:
            log.exception("provider failed")
        out = capsys.readouterr().out
        assert out.count("\n") >= 2
        assert key not in out
        assert "ts-also-secret-5678" not in out
        assert REDACTED in out
    finally:
        monkeypatch.undo()
        logging_config.configure_logging()


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", path, "1.1", 200),
        None,
    )


def test_access_log_filter_drops_the_query_string():
    record = _access_record("/api/operations/patients?query=David%20Davis")
    assert AccessLogQueryStringFilter().filter(record)
    message = record.getMessage()
    assert "Davis" not in message
    assert f"/api/operations/patients?{REDACTED}" in message


def test_access_log_filter_leaves_plain_paths_alone():
    record = _access_record("/api/health")
    AccessLogQueryStringFilter().filter(record)
    assert record.getMessage() == '127.0.0.1:5000 - "GET /api/health HTTP/1.1" 200'


def test_configure_logging_installs_the_access_filter_once():
    logging_config.configure_logging()
    logging_config.configure_logging()
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, AccessLogQueryStringFilter) for f in filters) == 1

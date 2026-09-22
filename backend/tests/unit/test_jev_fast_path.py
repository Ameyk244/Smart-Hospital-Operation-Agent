"""Unit tests for the Jev fast path (experimental, `jev-testing` branch).

No database, no network, no real `typesafe_sdk` — the `fake_typesafe`
fixture (tests/conftest.py) installs a fake module under the real package
name, so `app/agent/jev_fast_path.py` runs for real against a controllable
double. Same spirit as test_domain_gate.py: pure decision logic.

The single most important property under test is that *nothing* here can
raise into the request path. Every failure mode — unconfident answer, a
`none` choice, an API error, a timeout, and the SDK not being installed at
all — must come back as an ordinary non-matched result.
"""

import asyncio
import time

import pytest

from app.agent.jev_fast_path import (
    EXECUTABLE_COMMANDS,
    NONE_CHOICE,
    try_jev_fast_path,
)

# Unit tests only — no `pytestmark = pytest.mark.integration` here, so these
# run without Postgres.


# --- Confident matches ----------------------------------------------------


async def test_confident_match_builds_an_argument_free_command(
    fake_typesafe, jev_settings, jev_response
):
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.98)

    result = await try_jev_fast_path("what departments do you have?", jev_settings())

    assert result.matched is True
    assert result.command is not None
    assert result.command.name == "list_departments"
    assert result.command.args == {}
    assert result.choice == "list_departments"
    assert result.confidence == pytest.approx(0.98)
    assert result.failure_reason is None
    assert result.model == "jev-latest"
    assert result.input_tokens == 350
    assert result.output_tokens == 5


async def test_the_message_text_is_what_gets_sent_as_state(
    fake_typesafe, jev_settings, jev_response
):
    fake_typesafe.behavior = jev_response("list_departments")

    await try_jev_fast_path("what departments do you have?", jev_settings())

    assert fake_typesafe.calls[0]["state"] == "what departments do you have?"


async def test_the_api_key_is_passed_to_the_client_explicitly(
    fake_typesafe, jev_settings, jev_response
):
    """Regression test for a bug the mocked suite originally missed and the
    first live call caught. The SDK falls back to reading TYPESAFE_API_KEY
    from the environment when none is passed — but this project loads .env
    through pydantic-settings into `Settings`, which never exports anything
    into os.environ. So a bare TypeSafeClient() authenticates with nothing
    and fails instantly, while every mocked test still passes because the
    fake client doesn't care about credentials. The key must be handed over
    explicitly, and that is only assertable here."""
    fake_typesafe.behavior = jev_response("list_departments")

    await try_jev_fast_path("anything", jev_settings(typesafe_api_key="sentinel-key"))

    assert fake_typesafe.client_kwargs[0].get("api_key") == "sentinel-key"


async def test_every_criteria_label_is_executable_or_deliberately_not(
    fake_typesafe, jev_settings, jev_response
):
    """Guards the module's core invariant: Jev is only ever offered labels we
    can account for. Anything new in `criteria` must be either executable by
    CommandRunner, the explicit none-option, or the documented
    recognized-but-unsupported `search_patients`."""
    fake_typesafe.behavior = jev_response("list_departments")

    await try_jev_fast_path("anything", jev_settings())

    labels = set(fake_typesafe.calls[0]["questions"]["command"].criteria)
    assert labels == EXECUTABLE_COMMANDS | {NONE_CHOICE, "search_patients"}


async def test_an_explicit_none_option_is_always_offered(
    fake_typesafe, jev_settings, jev_response
):
    """TypeSafe's own guidance: a Choice should always include a "none"-style
    option so the model isn't forced onto the least-bad real label."""
    fake_typesafe.behavior = jev_response("list_departments")

    await try_jev_fast_path("anything", jev_settings())

    for question in fake_typesafe.calls[0]["questions"].values():
        assert NONE_CHOICE in question.criteria or "unspecified" in question.criteria


# --- Argument handling ----------------------------------------------------


async def test_a_confident_filter_answer_becomes_a_command_argument(
    fake_typesafe, jev_settings, jev_response
):
    fake_typesafe.behavior = jev_response(
        "list_scanners",
        confidence=0.96,
        filters={"scanner_type": ("MRI", 0.97), "scanner_status": ("AVAILABLE", 0.95)},
    )

    result = await try_jev_fast_path("which MRI machines are free?", jev_settings())

    assert result.matched is True
    assert result.command.name == "list_scanners"
    assert result.command.args == {"type": "MRI", "status": "AVAILABLE"}


async def test_an_unconfident_filter_is_omitted_not_guessed(
    fake_typesafe, jev_settings, jev_response
):
    """The rule that matters most about arguments: a filter we aren't sure of
    is dropped, widening the read-only result, rather than invented. A
    fabricated filter would silently return the *wrong* answer."""
    fake_typesafe.behavior = jev_response(
        "list_scanners",
        confidence=0.99,
        filters={"scanner_type": ("MRI", 0.4)},
    )

    result = await try_jev_fast_path("show me the scanners", jev_settings())

    assert result.matched is True
    assert result.command.args == {}


async def test_an_unspecified_filter_is_omitted(fake_typesafe, jev_settings, jev_response):
    fake_typesafe.behavior = jev_response(
        "list_delayed_appointments",
        confidence=0.99,
        filters={"appointment_type": ("unspecified", 0.99)},
    )

    result = await try_jev_fast_path("what's running late?", jev_settings())

    assert result.matched is True
    assert result.command.name == "list_delayed_appointments"
    assert result.command.args == {}


async def test_filter_answers_for_other_commands_are_ignored(
    fake_typesafe, jev_settings, jev_response
):
    """All four questions are asked in one round trip, so irrelevant answers
    always come back. They must never leak onto the chosen command."""
    fake_typesafe.behavior = jev_response(
        "list_departments",
        confidence=0.99,
        filters={"scanner_type": ("MRI", 0.99), "scanner_status": ("AVAILABLE", 0.99)},
    )

    result = await try_jev_fast_path("list the departments please", jev_settings())

    assert result.command.args == {}


async def test_delayed_appointments_uses_the_appointment_type_argument_name(
    fake_typesafe, jev_settings, jev_response
):
    """Regression guard: `list_delayed_appointments` reads `appointment_type`,
    not `type` — a `Command` this path builds must be one CommandRunner's
    handler actually understands."""
    fake_typesafe.behavior = jev_response(
        "list_delayed_appointments",
        confidence=0.99,
        filters={"appointment_type": ("CT", 0.98)},
    )

    result = await try_jev_fast_path("any late CT scans?", jev_settings())

    assert result.command.args == {"appointment_type": "CT"}


# --- Non-matches ----------------------------------------------------------


async def test_low_confidence_is_not_a_match(fake_typesafe, jev_settings, jev_response):
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.72)

    result = await try_jev_fast_path("tell me about this place", jev_settings())

    assert result.matched is False
    assert result.command is None
    assert result.failure_reason == "low_confidence"
    # Jev still ran, so this is not a failure — the caller must trace it as
    # REJECTED, not FAILURE.
    assert result.is_failure is False
    assert result.confidence == pytest.approx(0.72)


async def test_confidence_exactly_at_the_threshold_matches(
    fake_typesafe, jev_settings, jev_response
):
    """The documented rule is `>= threshold`; pinning the boundary here keeps
    a later refactor from quietly turning it into `>`."""
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.9)

    result = await try_jev_fast_path("departments?", jev_settings(jev_confidence_threshold=0.9))

    assert result.matched is True


async def test_none_choice_is_not_a_match(fake_typesafe, jev_settings, jev_response):
    fake_typesafe.behavior = jev_response(NONE_CHOICE, confidence=0.99)

    result = await try_jev_fast_path("reschedule my appointment", jev_settings())

    assert result.matched is False
    assert result.command is None
    assert result.choice == NONE_CHOICE
    assert result.failure_reason == "no_command_match"
    assert result.is_failure is False


async def test_search_patients_is_recognized_but_never_executed(
    fake_typesafe, jev_settings, jev_response
):
    """`search_patients` needs a free-text query, which a Choice cannot
    produce. It stays in the criteria so Jev has a correct place to put
    patient lookups, but it must never become a Command — the agent handles
    those, because it can read the name out of the message."""
    fake_typesafe.behavior = jev_response("search_patients", confidence=0.99)

    result = await try_jev_fast_path("find the patient called Maria Alvarez", jev_settings())

    assert result.matched is False
    assert result.command is None
    assert result.choice == "search_patients"
    assert result.failure_reason == "unsupported_command"
    assert result.is_failure is False


async def test_an_unknown_label_is_never_executed(fake_typesafe, jev_settings, jev_response):
    """Defense in depth: even if the API returned a label we never offered,
    it must not turn into a Command."""
    fake_typesafe.behavior = jev_response("drop_all_appointments", confidence=0.99)

    result = await try_jev_fast_path("anything", jev_settings())

    assert result.matched is False
    assert result.command is None
    assert result.failure_reason == "unknown_command"


async def test_a_malformed_response_is_not_a_match(fake_typesafe, jev_settings):
    class Garbage:
        answers = {}

    fake_typesafe.behavior = Garbage()

    result = await try_jev_fast_path("anything", jev_settings())

    assert result.matched is False
    assert result.failure_reason == "jev_malformed_response"
    # Must classify as a *failure*, not a decline. If the real SDK's response
    # shape ever differs from the documented one, every call lands here — and
    # as a decline it would be traced as REJECTED with no error_category,
    # i.e. "Jev wasn't confident" instead of "the integration is broken".
    assert result.is_failure is True


# --- Failure modes: none of these may raise -------------------------------


async def test_api_error_falls_through_without_raising(fake_typesafe, jev_settings):
    fake_typesafe.behavior = fake_typesafe.module.TypeSafeRateLimitError("slow down")

    result = await try_jev_fast_path("what departments do you have?", jev_settings())

    assert result.matched is False
    assert result.command is None
    assert result.failure_reason == "jev_api_error"
    assert result.is_failure is True


async def test_connection_error_subclasses_are_caught_by_the_base_class(
    fake_typesafe, jev_settings
):
    fake_typesafe.behavior = fake_typesafe.module.TypeSafeAPITimeoutError("upstream timeout")

    result = await try_jev_fast_path("what departments do you have?", jev_settings())

    assert result.failure_reason == "jev_api_error"


async def test_timeout_falls_through_without_raising(fake_typesafe, jev_settings):
    def _slow(_state, _questions):
        time.sleep(0.3)
        raise AssertionError("should have timed out before this returns")

    fake_typesafe.behavior = _slow

    result = await try_jev_fast_path(
        "what departments do you have?", jev_settings(jev_timeout_seconds=0.01)
    )

    assert result.matched is False
    assert result.failure_reason == "jev_timeout"
    assert result.is_failure is True
    # Allow the orphaned worker thread to finish so it can't trip a later
    # test; see the NOTE in try_jev_fast_path about wait_for not cancelling it.
    await asyncio.sleep(0.35)


async def test_an_unexpected_sdk_exception_falls_through_without_raising(
    fake_typesafe, jev_settings
):
    fake_typesafe.behavior = RuntimeError("something the SDK never documented")

    result = await try_jev_fast_path("what departments do you have?", jev_settings())

    assert result.matched is False
    assert result.failure_reason == "jev_unexpected_error"
    assert result.is_failure is True


async def test_missing_sdk_falls_through_without_raising(monkeypatch, jev_settings):
    """The package is an optional dependency and may simply not be installed.
    Note this test deliberately does *not* use `fake_typesafe` — it asserts
    the behavior when no `typesafe_sdk` exists at all."""
    import builtins
    import sys

    real_import = builtins.__import__

    def _no_typesafe(name, *args, **kwargs):
        if name == "typesafe_sdk":
            raise ImportError("No module named 'typesafe_sdk'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "typesafe_sdk", raising=False)
    monkeypatch.setattr(builtins, "__import__", _no_typesafe)

    result = await try_jev_fast_path("what departments do you have?", jev_settings())

    assert result.matched is False
    assert result.command is None
    assert result.failure_reason == "jev_sdk_missing"
    assert result.is_failure is True


async def test_a_missing_api_key_never_reaches_the_network(fake_typesafe, jev_settings):
    """Checked before the client is constructed, so a key-less environment
    can't produce a slow failing request — or an accidental live call."""
    result = await try_jev_fast_path(
        "what departments do you have?", jev_settings(typesafe_api_key=None)
    )

    assert result.matched is False
    assert result.failure_reason == "jev_missing_api_key"
    assert fake_typesafe.client_constructions == 0


async def test_the_flag_being_off_constructs_nothing(fake_typesafe, jev_settings):
    result = await try_jev_fast_path(
        "what departments do you have?", jev_settings(enable_jev_fast_path=False)
    )

    assert result.matched is False
    assert result.failure_reason == "disabled"
    assert fake_typesafe.client_constructions == 0
    assert fake_typesafe.calls == []


# --- SDK contract tolerance -----------------------------------------------


async def test_it_retries_without_the_model_kwarg_if_the_sdk_rejects_it(
    fake_typesafe, jev_settings, jev_response
):
    """The contract we were handed documents a default model but not a
    `model=` kwarg on `system_one`. Guessing wrong must degrade to the SDK's
    own default rather than hard-failing every call."""
    fake_typesafe.accepts_model = False
    fake_typesafe.behavior = jev_response("list_departments", confidence=0.99)

    result = await try_jev_fast_path("what departments do you have?", jev_settings())

    assert result.matched is True
    assert "model" not in fake_typesafe.calls[0]


async def test_null_token_usage_is_preserved_as_none(
    fake_typesafe, jev_settings, jev_response
):
    """`usage.input_tokens` is `int | None` in the SDK contract. A null must
    stay null so the cost endpoint can apply its documented estimate instead
    of a fabricated zero."""
    fake_typesafe.behavior = jev_response(
        "list_departments", confidence=0.99, null_usage=True
    )

    result = await try_jev_fast_path("what departments do you have?", jev_settings())

    assert result.matched is True
    assert result.input_tokens is None
    assert result.output_tokens is None


async def test_latency_is_measured(fake_typesafe, jev_settings, jev_response):
    response = jev_response("list_departments", confidence=0.99)

    def _slow(_state, _questions):
        time.sleep(0.05)
        return response

    fake_typesafe.behavior = _slow

    result = await try_jev_fast_path("what departments do you have?", jev_settings())

    assert result.matched is True
    assert result.latency_ms >= 40

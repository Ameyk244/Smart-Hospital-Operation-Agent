"""Unit tests for Jev's scope verdict (`classify_scope`).

Same setup as test_jev_fast_path.py: a fake `typesafe_sdk` installed under the
real name, so the production code runs for real against a controllable double.
The property under test that matters most: a verdict can block a message only
when Jev is confident it is not about the hospital; anything else, including
every failure, is "no block", and nothing here may raise.
"""

from app.agent.jev_fast_path import (
    ASSISTANT_CONVERSATION,
    HOSPITAL_OPERATIONS,
    NOT_HOSPITAL,
    SCOPE_QUESTION,
    classify_scope,
)
from tests.conftest import FakeJevAnswer, FakeJevResponse


def _verdict(label: str, confidence: float) -> FakeJevResponse:
    return FakeJevResponse({SCOPE_QUESTION: FakeJevAnswer(label, confidence)})


async def test_a_confident_not_hospital_verdict_blocks(fake_typesafe, jev_settings):
    fake_typesafe.behavior = _verdict(NOT_HOSPITAL, 0.99)

    result = await classify_scope("what is 47 times 12", jev_settings())

    assert (result.label, result.blocked, result.failure_reason) == (NOT_HOSPITAL, True, None)
    assert result.confidence == 0.99
    assert result.input_tokens == 350 and result.model == "jev-latest"


async def test_a_low_confidence_not_hospital_verdict_does_not_block(fake_typesafe, jev_settings):
    # "who is wei brown" measured at 0.34: a name is not a verdict.
    fake_typesafe.behavior = _verdict(NOT_HOSPITAL, 0.34)

    result = await classify_scope("who is wei brown", jev_settings())

    assert (result.label, result.blocked) == (NOT_HOSPITAL, False)


async def test_the_threshold_is_the_configured_one(fake_typesafe, jev_settings):
    fake_typesafe.behavior = _verdict(NOT_HOSPITAL, 0.85)

    assert not (await classify_scope("x", jev_settings(jev_confidence_threshold=0.9))).blocked
    assert (await classify_scope("x", jev_settings(jev_confidence_threshold=0.8))).blocked


async def test_in_scope_labels_never_block(fake_typesafe, jev_settings):
    for label in (HOSPITAL_OPERATIONS, ASSISTANT_CONVERSATION):
        fake_typesafe.behavior = _verdict(label, 1.0)

        result = await classify_scope("hello", jev_settings())

        assert (result.label, result.blocked) == (label, False)


async def test_only_the_scope_question_is_asked(fake_typesafe, jev_settings):
    fake_typesafe.behavior = _verdict(HOSPITAL_OPERATIONS, 1.0)

    await classify_scope("hello", jev_settings())

    assert list(fake_typesafe.calls[0]["questions"]) == [SCOPE_QUESTION]
    assert fake_typesafe.calls[0]["state"] == "hello"


async def test_the_previous_reply_is_included_and_bounded(fake_typesafe, jev_settings):
    fake_typesafe.behavior = _verdict(HOSPITAL_OPERATIONS, 1.0)

    await classify_scope("show me more", jev_settings(), previous_reply="x" * 5000)

    state = fake_typesafe.calls[0]["state"]
    assert state.startswith("Assistant's previous reply: " + "x" * 600 + "\n")
    assert "x" * 601 not in state
    assert state.endswith("User's new message: show me more")


async def test_no_verdict_when_jev_is_disabled_and_no_client_is_built(fake_typesafe, jev_settings):
    result = await classify_scope("hello", jev_settings(enable_jev_fast_path=False))

    assert (result.label, result.blocked, result.failure_reason) == (None, False, "disabled")
    assert fake_typesafe.client_constructions == 0


async def test_no_verdict_without_a_key(fake_typesafe, jev_settings):
    result = await classify_scope("hello", jev_settings(typesafe_api_key=None))

    assert (result.label, result.failure_reason) == (None, "jev_missing_api_key")
    assert fake_typesafe.client_constructions == 0


async def test_an_api_error_is_no_verdict_not_an_exception(fake_typesafe, jev_settings):
    fake_typesafe.behavior = fake_typesafe.module.TypeSafeAPIError("boom")

    result = await classify_scope("hello", jev_settings())

    assert (result.label, result.blocked, result.failure_reason) == (None, False, "jev_api_error")


async def test_an_unexpected_exception_is_no_verdict(fake_typesafe, jev_settings):
    fake_typesafe.behavior = RuntimeError("anything")

    result = await classify_scope("hello", jev_settings())

    assert (result.label, result.failure_reason) == (None, "jev_unexpected_error")


async def test_an_unreadable_or_unknown_answer_is_no_verdict(fake_typesafe, jev_settings):
    for response in (
        FakeJevResponse({}),
        FakeJevResponse({SCOPE_QUESTION: FakeJevAnswer("something_else", 1.0)}),
    ):
        fake_typesafe.behavior = response

        result = await classify_scope("hello", jev_settings())

        assert (result.label, result.blocked, result.failure_reason) == (None, False, "jev_malformed_response")

"""Voice phase 2: proves the full chain — audio file -> real local
transcription -> the exact same request routing a typed message goes
through -> a real `ChatResponse` — before any real-time/WebSocket code
exists. See docs/voice.md, Phase 2.

`tests/fixtures/audio/list_delayed_mri_appointments.wav` is a synthesized
("list delayed MRI appointments", Windows SAPI text-to-speech, 16kHz mono
WAV) fixture, checked in so this test needs no microphone and produces the
same transcript every run.

Transcription itself is real, local, and unmocked — `faster-whisper`
running on CPU, no network call, no cost. (The very first run on a machine
downloads the `tiny.en` model weights from Hugging Face once; after that
they're cached and every run is fully offline, same as any pip package
with model weights.) What *is* mocked, per this project's live-API policy,
is everything past the transcript: Jev and the agent model, using this
repo's existing `fake_typesafe`/`jev_settings`/`jev_response` fixtures and
the same `get_default_chat_model`/`run_agent` patch pattern
`test_chat_api.py` already uses — so this test proves the wiring is
correct on every run without spending real API credit on every run.

Goes through the real HTTP route (`client.post("/api/chat", ...)`), not a
direct call to `handle_chat_message()` — this also re-verifies Phase 1's
refactor (`POST /api/chat` -> `handle_chat_message()`) through the actual
API surface a real client would use, voice or text.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from app.voice.stt import transcribe

# Imported at module level on purpose — `test_chat_api.py` and
# `test_jev_fast_path_api.py` do the same. `app/main.py` calls
# `asyncio.set_event_loop_policy(...)` at import time (Windows/psycopg
# reason documented there). If that import happens lazily inside the
# `client` fixture instead, it lands *after* `db_session` has already
# opened an asyncpg connection bound to the current loop, and swapping the
# policy mid-test makes that connection fail with "attached to a different
# loop". Importing here forces it to happen at collection time.
from app.main import app  # noqa: F401,E402

pytestmark = pytest.mark.integration

FIXTURE_AUDIO = (
    Path(__file__).resolve().parent.parent / "fixtures" / "audio" / "list_delayed_mri_appointments.wav"
)


async def _transcribe_fixture() -> str:
    assert FIXTURE_AUDIO.exists(), f"missing checked-in fixture: {FIXTURE_AUDIO}"
    text = await transcribe(str(FIXTURE_AUDIO), model_size="tiny.en")
    # The point being proven: real speech-to-text produces real command-like
    # text, not that it matches one exact string forever (a model upgrade
    # could reasonably change punctuation/casing). Loosely shaped, not
    # byte-exact.
    assert "delayed" in text.lower()
    assert "appointment" in text.lower()
    return text


async def test_transcribed_voice_command_is_handled_by_jev(
    client, fake_typesafe, jev_settings, jev_response
):
    """The transcript doesn't match the parser's exact grammar (word order:
    "delayed MRI appointments", not "delayed appointments mri"), so this
    proves the realistic case: voice phrasing misses the strict parser and
    is picked up by the Jev fast path instead, ending in a real
    CommandRunner execution -- no LLM call."""
    transcript = await _transcribe_fixture()

    fake_typesafe.behavior = jev_response(
        "list_delayed_appointments",
        confidence=0.97,
        filters={"appointment_type": ("MRI", 0.95)},
    )

    with (
        patch("app.api.routes.chat.get_settings", return_value=jev_settings()),
        patch("app.api.routes.chat.get_default_chat_model") as mock_get_model,
    ):
        response = await client.post("/api/chat", json={"text": transcript})

    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "jev"
    mock_get_model.assert_not_called()
    # Real CommandRunner execution against the real seeded database, filtered
    # by the modality Jev extracted -- not a canned acknowledgment.
    for appointment in body["data"]:
        assert appointment["appointment_type"] == "MRI"


async def test_transcribed_voice_command_falls_through_to_the_agent(client, jev_settings):
    """The other real branch of the target architecture (voice -> STT ->
    parser miss -> Jev UNKNOWN -> agent): when Jev declines, the transcript
    still reaches the same agent a typed message would, through the same
    handle_chat_message() path -- proven here with a scripted model
    response, per this project's live-API policy."""
    transcript = await _transcribe_fixture()

    final_state = {
        "messages": [AIMessage(content="Both delayed MRI appointments are on SCN-3.")],
        "terminated_reason": None,
        "touched_entity_codes": ["APT-2001", "APT-2004", "SCN-3"],
    }

    with (
        patch(
            "app.api.routes.chat.get_settings",
            return_value=jev_settings(enable_jev_fast_path=False),
        ),
        patch("app.api.routes.chat.get_default_chat_model", return_value=object()),
        patch("app.api.routes.chat.run_agent", new=AsyncMock(return_value=final_state)) as run,
    ):
        response = await client.post("/api/chat", json={"text": transcript})

    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == "agent"
    assert body["message"] == "Both delayed MRI appointments are on SCN-3."
    assert body["touched_entity_codes"] == ["APT-2001", "APT-2004", "SCN-3"]
    run.assert_awaited_once()
    # The agent received the *transcribed* text, not a placeholder --
    # confirms the STT output is what actually reaches routing.
    assert run.call_args.kwargs["user_text"] == transcript

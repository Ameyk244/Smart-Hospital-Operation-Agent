"""WebSocket tests for streaming voice input (Phase 3, branch `voice`).

See docs/voice.md, "Testing" section, for the idiom this establishes:
Starlette's `TestClient.websocket_connect()`, a new pattern for this repo
(nothing else in `backend/tests/` tests a WebSocket endpoint).

Why this file does NOT use the shared `client`/`db_session`/
`seeded_session`/`agent_session_factory` fixtures from tests/conftest.py,
and instead builds its own DB plumbing:

`TestClient.websocket_connect()` runs the ASGI app inside its own
background thread with its OWN event loop (an anyio "blocking portal") --
confirmed by reading starlette/testclient.py, not assumed. Every other
async fixture in this repo creates its SQLAlchemy engine inside
pytest-asyncio's loop (the test function's own loop). Handing that engine
to code that actually runs inside the WebSocket portal's *different* loop
reproduces exactly the "attached to a different loop" asyncpg failure
tests/conftest.py's own `db_session` docstring warns about for a different
reason -- verified directly against this project's own Postgres while
building this file (a session_factory built in the pytest loop and reused
inside a `websocket_connect()` call fails every time with that exact
error).

So: this file's tests are plain sync `def test_...` functions (pytest only
manages a loop for `async def` tests; see pytest.ini's `asyncio_mode =
auto`), never using `with TestClient(app) as client:` (that form also runs
`app.main`'s real `lifespan`, which would build a real LangGraph
checkpointer against the *dev* database via `settings.database_url` --
exactly the kind of unapproved schema-touching action CLAUDE.md's
Destructive/DB-actions rule forbids; the `get_checkpointer`/
`get_session_factory` dependency overrides below make that unnecessary
regardless). `get_session_factory` is overridden per-test with a callable
that builds its SQLAlchemy engine *lazily*, on first actual use -- which
only ever happens from inside that test's own `websocket_connect()` call,
i.e. inside the portal thread's own loop, so every asyncpg connection it
ever opens is used from the one loop that created it. A second, throwaway
engine (created and disposed inside a plain `asyncio.run()`, never reused)
handles schema setup/seeding beforehand and DB assertions afterward --
each such call gets its own brand-new loop, which is fine: two *separate*
engines pointed at the same Postgres database from two different loops
never share a connection object, so there's nothing to conflict.

One more harness quirk this file works around, also verified directly
rather than assumed: `WebSocketTestSession.__exit__` sends the client-side
disconnect and then *cancels* the portal task shortly after, without
waiting for the server's disconnect-handling coroutine to finish first
(confirmed by tracing `app/api/routes/voice.py`'s own
`_record_dropped_connection` call: it started but never completed when
relying on the `with` block's own automatic cleanup for the "dropped
mid-utterance" scenario -- the resulting DB row was missing every time).
`test_dropped_connection_mid_utterance_records_event` below therefore
calls `ws.close()` itself and polls briefly for the resulting DB row
*before* letting the `with` block exit, rather than relying on that
built-in cleanup to happen in time.
"""

import asyncio
import sys
import time
import uuid
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

if sys.platform == "win32":
    # Same reason as tests/conftest.py and app/main.py: psycopg's async mode
    # refuses to run under Windows' default ProactorEventLoop. Set before
    # anything below opens a connection.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sqlalchemy import delete

from app.api.routes.chat import get_checkpointer
from app.config import Settings, get_settings
from app.db.base import Base
from app.db.models.agent import AgentEvent, AgentSession, ConversationMessage, EventStatus
from app.db.session import get_session_factory
from app.seed.seed_data import seed as seed_hospital_data

# Imported at module level on purpose -- see tests/integration/
# test_voice_stt_pipeline.py's identical comment and tests/conftest.py's
# db_session docstring for the Windows/psycopg event-loop trap this avoids:
# app.main sets the event loop policy at import time, which must happen
# before any DB fixture opens a connection.
from app.main import app  # noqa: F401,E402

pytestmark = pytest.mark.integration

FIXTURE_AUDIO = (
    Path(__file__).resolve().parent.parent / "fixtures" / "audio" / "list_delayed_mri_appointments.wav"
)

SAMPLE_RATE_HZ = 16_000


def _test_database_url() -> str:
    """Mirrors tests/conftest.py's `_test_database_url` (private there, not
    exposed as a fixture) -- the same `<configured_db>_test` database every
    other integration test uses, never the dev database."""
    settings = get_settings()
    base_url = settings.database_url
    prefix, db_name = base_url.rsplit("/", 1)
    return f"{prefix}/{db_name}_test"


async def _ensure_test_database_exists() -> None:
    import asyncpg

    settings = get_settings()
    prefix, db_name = settings.database_url.rsplit("/", 1)
    test_db_name = f"{db_name}_test"
    admin_url = f"{prefix}/postgres".replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(admin_url)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", test_db_name
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{test_db_name}"')
    finally:
        await conn.close()


async def _setup_test_database() -> None:
    """Schema + seed data, using its own engine that is fully disposed
    before returning -- deliberately never the loop `websocket_connect()`
    later runs the app in. See this module's docstring."""
    await _ensure_test_database_exists()
    engine = create_async_engine(_test_database_url(), echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            await seed_hospital_data(session)
            await session.commit()
    finally:
        await engine.dispose()


async def _cleanup_session_data(session_id: str) -> None:
    """Deletes everything this session's turn(s) committed -- AgentEvent,
    ConversationMessage, AgentSession, in FK-safe (children-first) order.

    Why this matters, and why no other integration test in this repo needs
    it: every other test's DB writes go through `db_session`/
    `seeded_session` (one connection, one transaction, rolled back at
    teardown -- see tests/conftest.py), so nothing they write ever survives
    the test. This file's tests genuinely `db.commit()` real rows (a
    necessary consequence of running inside `websocket_connect()`'s own
    portal thread/loop -- see this module's docstring), which would
    otherwise leak into the shared `_test` database permanently and corrupt
    an unrelated, unscoped assertion elsewhere in the suite --
    `test_jev_fast_path_api.py`'s cost-comparison tests aggregate
    `jev_invoked` events across the *whole* table when not scoped to a
    session_id, and did exactly that against a leftover row from this
    file's own Jev-branch test before this cleanup was added."""
    engine = create_async_engine(_test_database_url(), echo=False)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            await session.execute(
                delete(AgentEvent).where(AgentEvent.session_id == session_id)
            )
            await session.execute(
                delete(ConversationMessage).where(
                    ConversationMessage.session_id == session_id
                )
            )
            await session.execute(
                delete(AgentSession).where(AgentSession.id == session_id)
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _fetch_voice_dropped_events(session_id: str) -> list[AgentEvent]:
    engine = create_async_engine(_test_database_url(), echo=False)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            from app.api.routes.voice import VOICE_DROPPED_EVENT_TYPE

            result = await session.execute(
                select(AgentEvent).where(
                    AgentEvent.session_id == session_id,
                    AgentEvent.event_type == VOICE_DROPPED_EVENT_TYPE,
                )
            )
            return list(result.scalars().all())
    finally:
        await engine.dispose()


class _LazySessionFactory:
    """The `get_session_factory` override. Builds its engine only on first
    actual call -- see this module's docstring for why that matters."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._session_factory = None

    def __call__(self):
        if self._session_factory is None:
            engine = create_async_engine(self._url, echo=False)
            self._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        return self._session_factory


@pytest.fixture(scope="module")
def _voice_test_db() -> None:
    asyncio.run(_setup_test_database())


@pytest.fixture
def voice_client(_voice_test_db):
    """A plain `TestClient(app)`, never entered via `with TestClient(app) as
    client:` -- see this module's docstring on why that form (which runs
    app.main's real lifespan) is avoided entirely. `get_checkpointer` is
    stubbed to None -- same as tests/conftest.py's own `client` fixture,
    and for the same reason: `run_agent` treats `checkpointer=None` as
    "use ConversationMessage history instead", which is fine for every test
    here (none of them assert on LangGraph checkpoint persistence).
    `get_session_factory` gets a *fresh* `_LazySessionFactory` per test, not
    shared across tests -- reusing one across two separate
    `websocket_connect()` calls (each its own portal loop) reproduces the
    same cross-loop failure, verified directly while building this file.
    """
    app.dependency_overrides[get_checkpointer] = lambda: None
    app.dependency_overrides[get_session_factory] = _LazySessionFactory(
        _test_database_url()
    )
    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_checkpointer, None)
        app.dependency_overrides.pop(get_session_factory, None)


@pytest.fixture
def voice_session_id(voice_client):
    """A fresh session_id per test, cleaned up afterward -- see
    `_cleanup_session_data`'s docstring for why that cleanup is necessary
    here specifically. Depends on `voice_client` only for ordering (the
    session must be created, used, and cleaned up within the same
    dependency-override window); it doesn't use the client directly."""
    session_id = str(uuid.uuid4())
    yield session_id
    asyncio.run(_cleanup_session_data(session_id))


def _read_fixture_pcm() -> bytes:
    with wave.open(str(FIXTURE_AUDIO), "rb") as wav_file:
        assert wav_file.getframerate() == SAMPLE_RATE_HZ
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnchannels() == 1
        return wav_file.readframes(wav_file.getnframes())


def _chunk_bytes(data: bytes, n_chunks: int) -> list[bytes]:
    """Splits raw PCM bytes into `n_chunks` pieces on sample (2-byte)
    boundaries, simulating a client streaming audio over several WS
    frames rather than sending it all as one blob."""
    total_samples = len(data) // 2
    chunk_samples = total_samples // n_chunks
    chunks = []
    offset = 0
    for i in range(n_chunks):
        if i == n_chunks - 1:
            chunk = data[offset:]
        else:
            chunk = data[offset : offset + chunk_samples * 2]
            offset += chunk_samples * 2
        chunks.append(chunk)
    return chunks


def _silence_pcm(duration_ms: float) -> bytes:
    n_samples = int(SAMPLE_RATE_HZ * duration_ms / 1000)
    return b"\x00\x00" * n_samples


def _noise_pcm(duration_ms: float, *, amplitude: float, seed: int) -> bytes:
    """Synthetic PCM at a target normalized RMS of roughly `amplitude`
    (Gaussian noise has RMS approx. equal to its standard deviation). Used
    for VAD-focused tests that don't need real speech content -- only real
    STT output (the full-utterance test below) uses the checked-in fixture.
    """
    n_samples = int(SAMPLE_RATE_HZ * duration_ms / 1000)
    rng = np.random.default_rng(seed)
    samples = rng.normal(0.0, amplitude, n_samples).clip(-1.0, 1.0)
    return (samples * 32768.0).astype("<i2").tobytes()


def _drain_until_ready(ws, max_messages: int = 12) -> list[dict]:
    messages = []
    for _ in range(max_messages):
        message = ws.receive_json()
        messages.append(message)
        if message.get("type") == "ready":
            break
    return messages


def test_full_utterance_streamed_in_chunks_is_handled_by_jev(
    voice_client, voice_session_id, fake_typesafe, jev_settings, jev_response
):
    """The realistic end-to-end path: real audio, streamed in several WS
    frames (not one blob), real local transcription, real VAD state
    transitions, then routed through the same Jev fast path
    tests/integration/test_voice_stt_pipeline.py already proved for the
    HTTP path -- confirming voice and text converge on the exact same
    `handle_chat_message()` outcome. Picks the Jev branch (not the agent
    branch) because it also exercises a real `CommandRunner` execution
    against the real seeded DB, the stronger assertion; Phase 2 already
    covers both branches for the underlying routing logic, which this
    WebSocket layer does not reimplement."""
    session_id = voice_session_id
    pcm = _read_fixture_pcm()
    speech_chunks = _chunk_bytes(pcm, 5)
    # Trailing true-digital-silence, comfortably over the default
    # voice_vad_silence_ms (800ms), so speech_ended fires with "silence".
    trailing_silence = [_silence_pcm(250)] * 4

    fake_typesafe.behavior = jev_response(
        "list_delayed_appointments",
        confidence=0.97,
        filters={"appointment_type": ("MRI", 0.95)},
    )

    with patch("app.api.routes.chat.get_settings", return_value=jev_settings()):
        with voice_client.websocket_connect(f"/api/voice/{session_id}") as ws:
            assert ws.receive_json() == {"type": "ready"}

            for chunk in speech_chunks:
                ws.send_bytes(chunk)
            for chunk in trailing_silence:
                ws.send_bytes(chunk)

            messages = _drain_until_ready(ws)

    by_type = {m["type"]: m for m in messages}
    assert list(m["type"] for m in messages) == [
        "speech_started",
        "speech_ended",
        "transcribing",
        "transcript",
        "chat_response",
        "ready",
    ]
    assert by_type["speech_ended"]["reason"] == "silence"
    # Loosely shaped, like Phase 2's own assertion on this fixture -- real
    # STT output, not byte-exact forever.
    assert "delayed" in by_type["transcript"]["text"].lower()
    assert "appointment" in by_type["transcript"]["text"].lower()

    response = by_type["chat_response"]["response"]
    assert response["handled_by"] == "jev"
    assert response["session_id"] == session_id
    for appointment in response["data"]:
        assert appointment["appointment_type"] == "MRI"


def test_near_silence_never_triggers_speech(voice_client, voice_session_id):
    """No chunk ever crosses voice_vad_speech_rms_threshold, so the VAD must
    never leave IDLE. Asserted indirectly but rigorously: a connection drop
    while genuinely mid-utterance (SPEECH state) always writes a
    `voice_connection_dropped` row (see the dropped-connection test below,
    and app/api/routes/voice.py's `is_in_speech()` guard) -- so the absence
    of that row after this same drop sequence is direct proof the detector
    never confirmed speech, not merely an absence of a message this test
    happened not to look for."""
    session_id = voice_session_id
    quiet_chunks = [_noise_pcm(200, amplitude=0.001, seed=i) for i in range(5)]

    with voice_client.websocket_connect(f"/api/voice/{session_id}") as ws:
        assert ws.receive_json() == {"type": "ready"}
        for chunk in quiet_chunks:
            ws.send_bytes(chunk)

        # See module docstring: close explicitly and give the server a brief
        # moment to run its (in this case, near-instant, no-op-since-idle)
        # disconnect handling before the `with` block's own cleanup would
        # otherwise race it.
        ws.close()
        time.sleep(0.5)

    events = asyncio.run(_fetch_voice_dropped_events(session_id))
    assert events == []


def test_max_utterance_duration_forces_completion_not_error(voice_client, voice_session_id):
    """Continuous above-threshold audio, longer than a deliberately tiny
    voice_max_utterance_seconds, must still complete as a normal turn --
    speech_ended's reason differs (max_duration, not silence) but nothing
    is discarded and no error is raised. transcribe_array is mocked to a
    fixed, parser-matching string ("list departments") rather than relying
    on real Whisper output for synthetic noise, which would be both slow
    and unpredictable (Whisper does not reliably return empty text for
    noise -- verified while building this test -- so asserting "not an
    error" against real STT output on synthetic audio would be flaky by
    construction, not a meaningful VAD assertion)."""
    session_id = voice_session_id
    tiny_duration_settings = Settings(voice_max_utterance_seconds=0.6)
    # Continuous loud audio, no trailing silence -- total well over 0.6s.
    loud_chunks = [_noise_pcm(150, amplitude=0.15, seed=i) for i in range(10)]

    with (
        patch("app.api.routes.voice.get_settings", return_value=tiny_duration_settings),
        patch(
            "app.api.routes.voice.transcribe_array",
            new=AsyncMock(return_value="list departments"),
        ),
    ):
        with voice_client.websocket_connect(f"/api/voice/{session_id}") as ws:
            assert ws.receive_json() == {"type": "ready"}
            for chunk in loud_chunks:
                ws.send_bytes(chunk)
            messages = _drain_until_ready(ws)

    by_type = {m["type"]: m for m in messages}
    assert by_type["speech_ended"]["reason"] == "max_duration"
    assert "error" not in by_type
    assert by_type["chat_response"]["response"]["handled_by"] == "deterministic"
    assert messages[-1] == {"type": "ready"}


def test_dropped_connection_mid_utterance_records_event(voice_client, voice_session_id):
    """A connection dropped while genuinely mid-utterance (SPEECH state,
    non-empty buffer) must discard the buffer and record exactly one
    `voice_connection_dropped` AgentEvent -- failure mode 3 in
    app/api/routes/voice.py's module docstring."""
    session_id = voice_session_id
    loud_chunk = _noise_pcm(250, amplitude=0.15, seed=99)

    with voice_client.websocket_connect(f"/api/voice/{session_id}") as ws:
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(loud_chunk)
        assert ws.receive_json() == {"type": "speech_started"}

        # See module docstring: close explicitly and poll briefly for the
        # resulting DB row *before* letting the `with` block's own cleanup
        # run -- that cleanup cancels the server-side task shortly after
        # sending the disconnect, without waiting for it to finish handling
        # it first (confirmed by tracing the call while building this test).
        ws.close()
        deadline = time.monotonic() + 3.0
        events: list[AgentEvent] = []
        while time.monotonic() < deadline:
            events = asyncio.run(_fetch_voice_dropped_events(session_id))
            if events:
                break
            time.sleep(0.1)

    assert len(events) == 1
    event = events[0]
    assert event.status == EventStatus.FAILURE
    assert event.error_category == "connection_dropped_mid_utterance"
    assert event.arguments_json["buffered_ms"] > 0


def test_stt_failure_returns_error_and_connection_survives(voice_client, voice_session_id):
    """STT raising must not kill the connection: the current utterance gets
    `error: stt_failed` and the connection goes back to `ready`, still able
    to handle a subsequent utterance normally -- failure mode 2."""
    session_id = voice_session_id
    loud_chunk = _noise_pcm(300, amplitude=0.15, seed=7)
    trailing_silence = _silence_pcm(1000)

    call_count = {"n": 0}

    async def flaky_transcribe(samples, model_size):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("synthetic STT failure")
        return "list departments"

    with patch("app.api.routes.voice.transcribe_array", new=flaky_transcribe):
        with voice_client.websocket_connect(f"/api/voice/{session_id}") as ws:
            assert ws.receive_json() == {"type": "ready"}

            # First utterance: STT raises.
            ws.send_bytes(loud_chunk)
            ws.send_bytes(trailing_silence)
            first_turn = _drain_until_ready(ws)
            assert first_turn[-2] == {
                "type": "error",
                "code": "stt_failed",
                "message": "Speech-to-text failed for that utterance. Try again.",
            }
            assert first_turn[-1] == {"type": "ready"}

            # Second utterance, same connection: succeeds, proving the
            # connection survived the first failure.
            ws.send_bytes(loud_chunk)
            ws.send_bytes(trailing_silence)
            second_turn = _drain_until_ready(ws)

    assert any(m["type"] == "chat_response" for m in second_turn)
    assert second_turn[-1] == {"type": "ready"}
    assert call_count["n"] == 2


def test_misheard_acronym_is_corrected_before_routing_and_raw_is_kept(
    voice_client, voice_session_id
):
    """Whisper hears "MRI" as "MI". The transcript event carries the corrected
    text (plus what was actually heard), and the message that is routed is the
    corrected one: an exact deterministic MRI query, all results MRI and
    AVAILABLE -- not a modality-less list."""
    loud_chunk = _noise_pcm(300, amplitude=0.15, seed=7)
    trailing_silence = _silence_pcm(1000)

    async def fake_transcribe(samples, model_size):
        return "List scanners MI available."

    with patch("app.api.routes.voice.transcribe_array", new=fake_transcribe):
        with voice_client.websocket_connect(f"/api/voice/{voice_session_id}") as ws:
            assert ws.receive_json() == {"type": "ready"}
            ws.send_bytes(loud_chunk)
            ws.send_bytes(trailing_silence)
            messages = _drain_until_ready(ws)

    by_type = {m["type"]: m for m in messages}
    assert by_type["transcript"]["text"] == "List scanners MRI available."
    assert by_type["transcript"]["raw"] == "List scanners MI available."
    response = by_type["chat_response"]["response"]
    assert response["handled_by"] == "deterministic"
    assert response["data"], "seeded data has available MRI scanners"
    assert {(row["type"], row["status"]) for row in response["data"]} == {
        ("MRI", "AVAILABLE")
    }


def test_clean_transcript_has_no_raw_field(voice_client, voice_session_id):
    loud_chunk = _noise_pcm(300, amplitude=0.15, seed=7)
    trailing_silence = _silence_pcm(1000)

    async def fake_transcribe(samples, model_size):
        return "list departments"

    with patch("app.api.routes.voice.transcribe_array", new=fake_transcribe):
        with voice_client.websocket_connect(f"/api/voice/{voice_session_id}") as ws:
            assert ws.receive_json() == {"type": "ready"}
            ws.send_bytes(loud_chunk)
            ws.send_bytes(trailing_silence)
            messages = _drain_until_ready(ws)

    transcript = next(m for m in messages if m["type"] == "transcript")
    assert transcript == {"type": "transcript", "text": "list departments"}

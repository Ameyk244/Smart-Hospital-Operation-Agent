"""Streaming voice input over WebSocket (Phase 3, branch `voice`).

Audio contract (client -> server): binary WS frames, raw 16-bit signed PCM,
mono, little-endian, 16000 Hz, no envelope -- arbitrary-sized frames,
accumulated here. No JSON control messages from the client in this phase;
VAD alone drives state (see app/voice/vad.py).

Event contract (server -> client): JSON text frames --
`{"type": "ready"}`, `{"type": "speech_started"}`,
`{"type": "speech_ended", "reason": "silence" | "max_duration"}`,
`{"type": "transcribing"}`, `{"type": "transcript", "text": ...}`,
`{"type": "chat_response", "response": <ChatResponse.model_dump(mode="json")>}`,
`{"type": "error", "code": "empty_transcript" | "stt_failed" | "internal_error", "message": ...}`.
After a `chat_response` or `error`, `ready` is sent again and the connection
keeps listening -- one WebSocket connection carries multiple sequential
voice turns, it never closes itself after one.

The only thing this module does that a typed message doesn't is turn audio
into a final transcript. Once it has one, it calls
`app.api.routes.chat.handle_chat_message()` -- the one routing
implementation (parser -> domain gate -> eligibility -> Jev -> agent) -- and
never reimplements any part of that. See that module's docstring and
docs/voice.md.

Session lifecycle: `get_session_factory`/`get_checkpointer` (the exact
dependencies `chat.py` already uses -- see that module's `get_checkpointer`
for why it's typed `HTTPConnection`, not `Request`, which is what makes
reusing it here possible at all) are resolved once per connection. A fresh
`session_factory()` session is opened per discrete unit of work -- once per
`handle_chat_message()` call, and once for the connection-drop trace event
-- never one session held for the whole (potentially long-lived, arbitrary-
idle-time) connection. Mirrors why the agent graph's tool_node opens a
fresh DB session per tool call rather than one for the whole run (see
app/agent/graph.py's module docstring).

Failure modes, each handled explicitly (see docs/voice.md's "Known
limitations" section):
  1. Empty/unusable transcript -> `error: empty_transcript`, never calls
     `handle_chat_message` with empty text.
  2. STT itself raises -> caught, `error: stt_failed`. Never lets a
     `faster-whisper` exception kill the connection.
  3. Connection drops mid-utterance (WebSocketDisconnect while VAD is in
     SPEECH state) -> the in-memory buffer is discarded (no attempt to
     salvage a partial transcript) and a `VOICE_DROPPED_EVENT_TYPE` trace
     event is recorded, so this genuinely-new, non-durable, in-memory
     state's loss isn't invisible. A drop while idle needs no event --
     nothing was lost.
  4. Max-utterance-duration cutoff is NOT a failure -- a successful
     completion forced by duration instead of silence; still transcribed
     and routed normally, only `speech_ended`'s `reason` differs.
  5. Any other unexpected exception while handling one utterance degrades
     to `error: internal_error` and returns to `ready`, rather than
     crashing the WebSocket -- the same "never surface as a failed turn"
     principle app/agent/jev_fast_path.py already follows for its own
     third-party call.

What calls it: app/main.py includes this router. Phase 4 (frontend mic
control, a separate subagent) is built against exactly this event contract.
"""

import numpy as np
from fastapi import APIRouter, Depends
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.api.routes.chat import get_checkpointer, handle_chat_message
from app.config import Settings, get_settings
from app.db.models.agent import EventStatus
from app.db.repositories.session_repository import SessionRepository
from app.db.session import get_session_factory
from app.observability.logging_config import get_logger
from app.observability.tracing import record_event
from app.voice.stt import transcribe_array
from app.voice.vad import SpeechEnded, SpeechStarted, UtteranceDetector

_logger = get_logger("voice.websocket")

# The trace event type a mid-utterance connection drop writes. Same
# convention as JEV_EVENT_TYPE in app/api/routes/chat.py: a named constant
# so the contract is greppable rather than a string literal repeated across
# call sites (there's only one call site today, but the constant documents
# the contract regardless).
VOICE_DROPPED_EVENT_TYPE = "voice_connection_dropped"

router = APIRouter(prefix="/api/voice", tags=["voice"])


def _make_detector(settings: Settings) -> UtteranceDetector:
    return UtteranceDetector(
        speech_rms_threshold=settings.voice_vad_speech_rms_threshold,
        silence_ms=settings.voice_vad_silence_ms,
        min_speech_ms=settings.voice_vad_min_speech_ms,
        max_utterance_ms=settings.voice_max_utterance_seconds * 1000.0,
    )


def _pcm_bytes_to_normalized_array(audio: bytes) -> np.ndarray:
    """int16 PCM bytes -> float32 samples normalized to [-1, 1] -- the exact
    conversion the brief specifies (divide by 32768.0), and the format
    `transcribe_array` expects."""
    return np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768.0


async def _record_dropped_connection(
    session_factory, session_id: str, buffered_ms: float
) -> None:
    """Its own fresh DB session, opened here rather than reused from
    anywhere else -- the connection itself is already gone by the time this
    runs. Ensures the session row exists first: a connection can drop
    during its very first utterance, before `handle_chat_message` has ever
    run `SessionRepository.get_or_create`, and `AgentEvent.session_id` is a
    foreign key into `agent_sessions` -- recording the event without this
    would fail the insert instead of documenting the loss."""
    async with session_factory() as db:
        await SessionRepository(db).get_or_create(session_id)
        await record_event(
            db,
            session_id=session_id,
            round_num=0,
            event_type=VOICE_DROPPED_EVENT_TYPE,
            status=EventStatus.FAILURE,
            arguments={"buffered_ms": int(round(buffered_ms))},
            error_category="connection_dropped_mid_utterance",
        )
        await db.commit()


async def _process_utterance(
    audio: bytes,
    *,
    websocket: WebSocket,
    session_id: str,
    session_factory,
    checkpointer,
    settings: Settings,
) -> None:
    """Transcribe one finalized utterance and route it through the exact
    same request handling a typed message goes through. Every early return
    here is a handled failure mode (empty transcript / STT failure); any
    exception that escapes this function is something else and is caught by
    the caller's broader `except Exception` -- see that function's
    docstring for why the split is deliberate."""
    samples = _pcm_bytes_to_normalized_array(audio)
    if samples.size == 0:
        # Defensive: unreachable in practice given UtteranceDetector only
        # ever finalizes a non-empty buffer (min_speech_ms > 0 guarantees
        # at least one above-threshold chunk was captured), but "the buffer
        # somehow had no real audio" is an explicitly named failure mode in
        # the brief, so it's checked rather than assumed away.
        await websocket.send_json(
            {
                "type": "error",
                "code": "empty_transcript",
                "message": "No audio was captured for that utterance.",
            }
        )
        return

    await websocket.send_json({"type": "transcribing"})

    try:
        text = await transcribe_array(samples, settings.voice_stt_model)
    except Exception as exc:  # noqa: BLE001 - third-party STT call, see module docstring
        _logger.warning("voice_stt_failed", error=type(exc).__name__)
        await websocket.send_json(
            {
                "type": "error",
                "code": "stt_failed",
                "message": "Speech-to-text failed for that utterance. Try again.",
            }
        )
        return

    text = text.strip()
    if not text:
        await websocket.send_json(
            {
                "type": "error",
                "code": "empty_transcript",
                "message": "Couldn't make out any speech in that utterance.",
            }
        )
        return

    await websocket.send_json({"type": "transcript", "text": text})

    async with session_factory() as db:
        response = await handle_chat_message(
            text=text,
            session_id=session_id,
            db=db,
            session_factory=session_factory,
            checkpointer=checkpointer,
        )

    await websocket.send_json(
        {"type": "chat_response", "response": response.model_dump(mode="json")}
    )


@router.websocket("/{session_id}")
async def voice_stream(
    websocket: WebSocket,
    session_id: str,
    session_factory=Depends(get_session_factory),
    checkpointer=Depends(get_checkpointer),
) -> None:
    settings = get_settings()
    await websocket.accept()
    detector = _make_detector(settings)
    await websocket.send_json({"type": "ready"})

    while True:
        try:
            chunk = await websocket.receive_bytes()
        except WebSocketDisconnect:
            # Dropped while idle: nothing was lost, no event to record.
            # Dropped mid-utterance: the VAD's in-memory buffer is
            # genuinely, non-durably lost -- record that explicitly (failure
            # mode 3, see module docstring) rather than let it vanish
            # silently.
            if detector.is_in_speech():
                await _record_dropped_connection(
                    session_factory, session_id, detector.buffered_ms()
                )
            return

        try:
            event = detector.ingest(chunk)

            if isinstance(event, SpeechStarted):
                await websocket.send_json({"type": "speech_started"})

            elif isinstance(event, SpeechEnded):
                await websocket.send_json(
                    {"type": "speech_ended", "reason": event.reason.value}
                )
                await _process_utterance(
                    event.audio,
                    websocket=websocket,
                    session_id=session_id,
                    session_factory=session_factory,
                    checkpointer=checkpointer,
                    settings=settings,
                )
                await websocket.send_json({"type": "ready"})

        except WebSocketDisconnect:
            if detector.is_in_speech():
                await _record_dropped_connection(
                    session_factory, session_id, detector.buffered_ms()
                )
            return
        except Exception as exc:  # noqa: BLE001 - see module docstring, failure mode 5
            _logger.exception("voice_websocket_unexpected_error", error=type(exc).__name__)
            try:
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "internal_error",
                        "message": "Something went wrong handling that. Please try again.",
                    }
                )
                await websocket.send_json({"type": "ready"})
            except Exception:
                # The socket itself is no longer usable -- nothing left to
                # do but stop handling this connection.
                return

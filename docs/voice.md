# Voice Input (Streaming) — Experimental, Branch `voice`

Streaming voice input alongside the existing text pipeline. Not merged to
`master`. This file is filled in as each phase lands, not reconstructed
afterward — sections marked `(pending)` haven't been built yet.

## Why streaming, not record-and-send

A simple record-and-send design (capture audio, send one blob, get one
transcript back) would have been the shorter path to "voice input works."
This project deliberately chose full streaming instead — live mic chunks
over a persistent connection, VAD-based utterance detection — because the
goal is to understand how a real streaming voice pipeline works, not just
to get voice input working the shortest way possible. That makes this a
genuine new architectural layer, built as its own phased slice rather than
end-to-end in one pass, the same instinct as the original master prompt's
thin-vertical-slice approach.

## Architecture

```text
TEXT                         VOICE
  │                            │
  │                            ▼
  │                           STT
  │                            │
  │                     FINAL TRANSCRIPT
  │                            │
  └────────────┬───────────────┘
               ▼
             PARSER
          ┌────┴────┐
       KNOWN      UNKNOWN
         │            │
         ▼            ▼
      RUNNER         JEV
                  ┌────┴────┐
                KNOWN     UNKNOWN
                  │          │
                  ▼          ▼
                RUNNER      AGENT
```

Voice and text converge into the exact same code path with no duplicated
routing logic — both call `handle_chat_message()` (extracted from
`app/api/routes/chat.py` in Phase 1). The WebSocket handler's only job is
producing a final transcript string; everything after that is identical to
what a typed message goes through, including the domain gate, the Jev fast
path, grounding, and the agent.

## Decisions and why

### Transport: WebSocket in the same FastAPI app
Confirmed in the pre-build audit by reading `app/main.py`: this is a single
`FastAPI()` instance with `app.include_router(...)` per route module and one
process-lifetime `lifespan`. A WebSocket router is added the same way —
`app/api/routes/voice.py`'s `APIRouter`, included in `main.py` alongside the
existing ones. No second process or port.

### VAD: energy-based (RMS), not Silero — deliberately deferred
Silero VAD's packaged pip install requires `torch>=1.12` and `torchaudio`
even for ONNX-runtime usage, because its wrapper uses `torchaudio` for I/O
(a `silero-vad-notorch` package exists but wasn't evaluated in depth). That
is a genuinely heavy addition — nothing else in this stack touches torch.
A simple RMS-energy threshold needs no new dependency and is enough to
detect utterance start/stop for short command-length audio in a reasonably
quiet environment, which is this project's actual use case. Silero is the
documented upgrade path if energy-based endpointing proves too noisy in
real testing — that's a real trade to make later with evidence, not a
default to reach for now.

### STT: local `faster-whisper`, not a hosted streaming API
Both were cheap enough that cost wasn't the deciding factor: `faster-whisper`
is free per call once installed; Deepgram Nova-3 streaming was ~$0.0077/min
at time of writing (~$0.0006 for a 5-second command) — negligible either
way. The deciding factor was consistency with this project's established
philosophy: every prior design decision (deterministic parser first, the
domain gate, the Jev fast path) has been about avoiding unnecessary paid
calls and staying runnable offline except for the live LLM/Jev calls
already in the project (see `docs/PROGRESS.md`'s first architectural
decision). A hosted STT API would be a third metered vendor alongside
Anthropic and TypeSafe; local Whisper keeps the "offline except live model
calls" property intact.

`faster-whisper`'s actual PyPI dependency list (verified against its
metadata, not assumed): `ctranslate2`, `huggingface-hub`, `tokenizers`,
`onnxruntime`, `av`, `tqdm` — no torch in the base install.

**Model: `tiny.en`, CPU, int8** (`settings.voice_stt_model`). Measured
directly against this project's own checked-in fixture
(`tests/fixtures/audio/list_delayed_mri_appointments.wav`, a synthesized
"list delayed MRI appointments"), not assumed from the model card:

| | `tiny.en` | `base.en` |
|---|---|---|
| Warm model load (once per process) | ~1.0s | ~1.5s |
| Transcription, per utterance | ~420–530ms | ~600–820ms |
| Transcript | `"List delayed MRI appointments."` | `"List delayed MRI appointments"` |

Both transcribed the fixture correctly. `tiny.en` is faster with no
accuracy loss on short, clear command audio — this project's actual use
case, not long-form dictation — so it's the default. Both are well inside
the "a few hundred ms to ~1-2s" target for a responsive command interface.
The model loads once per process (like the LangGraph checkpointer in
`app/main.py`'s `lifespan`), not per request; only the first-ever run on a
machine needs network access, to download the model weights once from
Hugging Face — after that, transcription is fully offline.

### Convergence point: `handle_chat_message()`
`chat.py`'s `chat()` route previously inlined everything from session
lookup through parser → domain gate → eligibility → Jev → agent → response
in one function. Phase 1 extracts that body into a plain, directly callable
`handle_chat_message()`, and `POST /api/chat` becomes a thin wrapper around
it. The WebSocket handler calls the same function once it has a final
transcript. This is the same discipline as `CommandRunner`: one trusted
implementation, multiple entrances — never a second routing path.

### Session lifecycle: reuse `session_id` as-is
LangGraph's checkpoint `thread_id` is already set directly to
`configurable={"thread_id": session_id}` (`app/agent/graph.py`) — the app's
`session_id` *is* the checkpoint thread id, with no separate mapping layer.
A voice WebSocket session reuses the same `session_id` the client already
keeps (the frontend's existing `sessionStorage` key), so a voice command and
a typed follow-up in the same session automatically share conversation
history, preferences, and grounding. No new session concept was introduced.

## Known limitations (accepted for this phase, not oversights)

- Energy-based VAD is noise-sensitive. It is expected to false-trigger in a
  loud environment; Silero is the documented upgrade path if that happens
  in real testing, not a fallback built in advance of evidence.
- A dropped WebSocket connection mid-utterance discards the in-memory audio
  buffer cleanly rather than attempting to recover a partial transcript.
- A max-utterance-duration safety cutoff exists so a client that never
  signals end-of-speech can't hold a connection (and its buffer) open
  indefinitely — the same bounded-execution instinct as
  `MAX_AGENT_ROUNDS`/`MAX_TOOL_CALLS` elsewhere in this codebase, applied to
  a new kind of unbounded loop.
- The per-connection audio buffer is genuinely new: unlike everything else
  in this backend, which is either a DB row or a LangGraph checkpoint, it is
  in-memory and non-durable. A connection drop or buffer discard emits an
  explicit trace/log event for exactly this reason — so the failure mode is
  visible, not silently invisible.
- Silero VAD and a hosted-STT comparison are explicitly out of scope for
  this build.

## Phases

### Phase 1 — Extract the shared handler ✅
`backend/app/api/routes/chat.py`'s `handle_chat_message()` is the one
routing implementation; `POST /api/chat` is a thin wrapper around it.
Verified as a true no-op refactor: the offline suite's pass count was
identical before and after (216 passed, 6 skipped both times).

### Phase 2 — Fixed-file STT → pipeline proof ✅
`backend/app/voice/stt.py` (`transcribe()`) wraps `faster-whisper`.
`backend/tests/integration/test_voice_stt_pipeline.py` transcribes the
checked-in fixture with the real, local, unmocked STT model, then POSTs
the transcript through the real `/api/chat` route (Phase 1's actual thin
wrapper, not a shortcut around it) with Jev and the agent model mocked —
one test per branch of the target architecture's UNKNOWN path: a
confident Jev match (`list_delayed_appointments`, filtered to MRI, which
Jev extracted from the transcript) executing through the real
`CommandRunner`, and a Jev decline falling through to a scripted agent
response. Both pass; full suite 218 passed (216 + 2), 6 skipped, ruff
clean.

### Phase 3 — WebSocket + live VAD `(pending)`
### Phase 4 — Frontend mic control `(pending)`

## Testing

Two idioms now exist in `backend/tests/`:
- **Real local STT, mocked everything downstream** (Phase 2, established):
  `faster-whisper` runs for real (no cost, no network after the first
  model download) against a checked-in fixture audio file; Jev and the
  agent model are mocked exactly like every other test in this project,
  per the live-API-minimization policy.
- **WebSocket testing** `(pending — Phase 3 records the
  `TestClient.websocket_connect()` pattern here, a new idiom for this
  repo; nothing else in `backend/tests/` currently tests a WebSocket
  endpoint.)`

## Configuration

```dotenv
VOICE_STT_MODEL=tiny.en
VOICE_MAX_UTTERANCE_SECONDS=15.0
```

No new secret or paid API for this phase — local STT only.

## Source Map

| Concern | File |
|---|---|
| Shared routing implementation | `backend/app/api/routes/chat.py` (`handle_chat_message`) |
| Local speech-to-text | `backend/app/voice/stt.py` |
| Settings | `backend/app/config.py` (`voice_stt_model`, `voice_max_utterance_seconds`) |
| Fixed-file pipeline proof | `backend/tests/integration/test_voice_stt_pipeline.py` |
| Test fixture audio | `backend/tests/fixtures/audio/list_delayed_mri_appointments.wav` |
| WebSocket endpoint | `(pending — Phase 3)` |
| Frontend mic control | `(pending — Phase 4)` |

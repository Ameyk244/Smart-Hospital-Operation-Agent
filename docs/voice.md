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

## Findings from the first end-to-end voice test matrix

16 synthesized spoken commands (Windows SAPI "Microsoft David", 16kHz, padded
with silence) were fed through a real Chromium's fake mic into the real running
app — real mic button, real capture and resampling, real WebSocket, real
`tiny.en`, real Jev and agent — recording the exact `transcript` and
`chat_response` frames and the badge the UI rendered. Nothing was mocked and
nothing was fixed during that pass; it existed to list problems.

| Finding | Kind | Outcome |
|---|---|---|
| Trailing `.`/`?` breaks the `$`-anchored parser rules; `show patient David Davis.` searched for `"David Davis."` and found nobody | parser bug, **not voice-specific** | **fixed** (below) |
| "why is scanner four unavailable" → "Why scanner for unavailable?" — the only transcription error that changed what the user got | STT accuracy | comparison below |
| "list scanners" → "List Scanner's …" (possessive) misses the grammar; Jev still answered correctly | STT, cost only | left as-is |
| "show me who's free for MRI right now" → Jev 0.86, under the 0.90 threshold, so the agent answered (correctly) | routing threshold, cost only | left as-is |
| "S C N seven" → `SCN7` (no hyphen) — the agent coped | STT | left as-is |

The other ten commands were word-exact. Every DOM badge matched the wire's
`handled_by`, no console errors, database unchanged. The audio is one clean
synthetic voice with no noise or accent, so read these as an optimistic floor
for accuracy, not a forecast for a real microphone.

### Fix: trailing punctuation no longer breaks the deterministic parser
Whisper appended a trailing `.` or `?` to 13 of the 16 transcripts (every one
from command 4 onward), and typed
sentences end in one just as often, so this was never a voice corner case. It
also wasn't luck-dependent in the harmless direction: two of the four
"deterministic" commands only passed because Whisper happened to omit the
period that run.

Confirmed *before* the fix by typing into the real UI (no audio involved):
`list departments.` and `show next appointment.` missed the parser and took a
paid Jev detour, and `show patient David Davis.` returned **"No patients matched
that search."** under a `DETERMINISTIC` badge. That last one is the dangerous
shape — a confidently wrong empty result, where a mishearing at least tends to
produce an obviously confused answer.

`parse()` now normalizes first: repeated whitespace collapses and trailing
sentence punctuation (`. ? ! , ; : …`) is dropped. It lives in `parse()`
because that is the one function the chat route (typed and voice alike),
`/api/commands`, and the agent's `execute_command` all share — one fix, not one
per entrance. Only trailing punctuation goes; `O'Neil-Smith` is untouched, and
the grammar is no more fuzzy than before (`list departments please.` still
misses, by design). 112 new test cases — parametrized across every grammar
shape and twelve suffixes, plus DB-backed end-to-end tests that
`show patient David Davis.` finds exactly the one patient the unpunctuated form
does. Those tests were run against the old parser first and fail there (53
failures), so they demonstrably catch the bug rather than merely passing.

### Whisper model comparison: `tiny.en` vs `base.en` vs `small.en` (decision left open)

Triggered by matrix case #16, where `why is scanner four unavailable` was heard
as `Why scanner for unavailable?` — an outcome-changing error, since it turns
scanner 4 into the word "for". **The default is unchanged (`tiny.en`); this
records the tradeoff so the choice is made deliberately, not silently.**

Method: 36 synthesized clips (12 short scanner/number-heavy phrases × 3 Windows
TTS voices), run through the production function
`app.voice.stt._transcribe_array_sync` (CPU, int8, beam 5) with the same
0.2s-lead / 0.8s-trailing-silence padding the VAD hands over. Two passes per
clip, faster kept. WER is computed after lowercasing, stripping punctuation and
mapping digits to words (so `4` = `four`).

| Model | Exact transcripts | WER | Median latency | p90 | Model load |
|---|---|---|---|---|---|
| `tiny.en` (default) | 27 / 36 | 5.6% | 404 ms | 426 ms | 1.1 s |
| `base.en` | 30 / 36 | 3.3% | 757 ms | 829 ms | 0.7 s |
| `small.en` | 33 / 36 | 1.7% | 2690 ms | 3107 ms | 49.5 s (first download) |

What the numbers hide — the actual errors:

- **`base.en` did not fix #16.** `four` was still heard as `for` for two of the
  three voices. `small.en` did fix it.
- **`base.en` introduced its own number errors**: `eight` → `aid` (`Show scanner
  aid status`) for two voices, which `tiny.en` got right for those voices.
- `tiny.en` mishears `two` as `too` (`scanner too available`), which `base.en`
  gets right. Homophones are the dominant failure for both small models.
- `for MI` / `for our CT` (`free for MRI` / `are for CT`) appeared in all three.

One live check through the real browser mic path (Chromium fake mic → WebSocket
→ backend, one stack per model): `base.en` latency in the app was ~0.8 s
against ~0.5 s for `tiny.en`, matching the offline figures, and the transcripts
broadly matched the offline ones. Seven phrases per model; one `base.en` run was cut
off by an interruption, so it is six pairs.

**Reading it:** `base.en` buys ~2 fewer bad transcripts in 12 for ~350 ms more
per utterance, but trades some errors for others rather than removing the
number-word class. `small.en` is materially better but its ~2.7 s median is
long enough to feel laggy on top of the 800 ms silence endpoint. None of the
three is safe to act on for a write by transcript alone; that is why voice
input still shows the transcript and routes through the same grounded,
read-first pipeline as text. To switch, set `VOICE_STT_MODEL=base.en` (no code
change).

### Fix: mishearings of domain words no longer reach routing or memory

The 40-message spoken pass showed one class of error behind three symptoms:
Whisper turns domain words into other words and everything downstream trusts
the transcript.

| Heard | Should be | What it cost |
|---|---|---|
| `which MI scanners are available` | MRI | Jev dropped the modality filter and answered with 5 scanners including CT and X-ray, confidently, no hedge |
| `Remember that I prefer MI scanner 1` | MRI | the wrong value was saved as a preference |
| `For Get My Scanner Preference` | forget | the request was read as "list", nothing was forgotten |

`app/voice/vocab.py` is one short, explicit list of anchored regex rules run
once per utterance, right after transcription and before the parser, domain
gate, Jev or any memory tool sees the text. Rules: `MI`/`MRR` and spelled-out
`M R I`/`M.R.I.` -> `MRI`; `C T`/`C.T.` -> `CT`; `x-ray`/`x ray` -> `xray` (the
parser's word); `for get` -> `forget`; `pointment` -> `appointment`;
`scanner aid` -> `scanner eight`; `scanner too <status word>` -> `scanner two`.
The transcript event still carries what Whisper heard, as `raw`, whenever it
was changed, so a correction is visible rather than silent.

Deliberately narrow: no fuzzy matching, no model. A rule exists only for a
mishearing that was observed (or its direct sibling) and whose corrected form
could not be a legitimate different word here. Left uncorrected on purpose:
`scanner for unavailable` (`four` vs real English `for` -- `which scanners are
for CT` is valid), `is the scanner too busy` (no status word after `too`), and
`MI` after `patient`/`named` (a name). Scanner-number homophones therefore
remain the model's weak spot; the answer to those is a bigger model (see the
comparison above), not a longer table.

Voice only. Typed text is what the person meant, and typing does not produce
these errors, so rewriting it would be a surprise with nothing to gain.

Related finding, not fixed here: the deterministic parser is lenient in the
same dangerous direction. `list scanners MI available` *matches* and runs as
`list_scanners(status=AVAILABLE)`, silently ignoring `MI`; `list scanners
banana` matches and lists every scanner; and its `ct`/`mri` test is a
substring check on the tail, so a word like `connected` would read as `CT`.
The correction above removes the voice-sourced trigger, but a typed `MI` still
gets the wrong answer deterministically. The parser rejecting an unrecognised
tail (letting Jev or the agent handle it) is the right fix; it changes the
grammar, so it is left for a decision.

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

### Phase 3 — WebSocket + energy-based VAD ✅
`backend/app/api/routes/voice.py` adds `@router.websocket("/api/voice/{session_id}")`,
included in `app/main.py` alongside the existing routers. Binary WS frames
(raw 16-bit PCM, mono, 16kHz, no envelope) accumulate into
`app/voice/vad.py`'s `UtteranceDetector` — a chunk-level, duration-based
(not wall-clock) RMS state machine: IDLE, buffering a candidate run once
RMS crosses `voice_vad_speech_rms_threshold`, confirmed as SPEECH once that
run reaches `voice_vad_min_speech_ms`, finalized once a silence run reaches
`voice_vad_silence_ms` or total duration reaches `voice_max_utterance_seconds`
(the latter is a successful completion forced by duration, `reason:
"max_duration"`, not a failure). A finalized utterance is converted to a
normalized float32 array and transcribed via `app/voice/stt.py`'s new
`transcribe_array()` (a sibling to Phase 2's file-based `transcribe()`,
same cached model loader, same thread-offload pattern, file-based path
untouched) and routed through the exact same `handle_chat_message()` Phase
1 extracted — no reimplemented routing.

All three named failure modes are handled explicitly: an empty/blank
transcript never reaches `handle_chat_message`; an STT exception is caught
and reported as `error: stt_failed` without killing the connection; a
connection dropped while genuinely mid-utterance (SPEECH state) discards
the in-memory buffer and records a `voice_connection_dropped` `AgentEvent`
(status `FAILURE`, `error_category="connection_dropped_mid_utterance"`,
`arguments={"buffered_ms": ...}`) so that loss is visible rather than
silent — a drop while idle needs no event, since nothing was lost. Any
other unexpected exception degrades to `error: internal_error` and returns
to `ready` rather than crashing the socket, the same principle
`jev_fast_path.py` already follows for its own third-party call. One WS
connection handles multiple sequential voice turns; it never closes itself
after one.

`get_session_factory`/`get_checkpointer` are resolved once per connection
and a fresh `session_factory()` session is opened per discrete unit of
work (once per `handle_chat_message()` call, once for a drop event) — never
one session held for the connection's whole, potentially long-lived,
lifetime. This required one small, deliberate change to the *existing*
`get_checkpointer` in `app/api/routes/chat.py`: its `request` parameter is
now typed `HTTPConnection` (the common base of `Request` and `WebSocket`)
instead of `Request`. This was not optional — verified directly against
FastAPI's own dependency resolution (`fastapi/dependencies/utils.py`'s
`solve_dependencies`): a `Request`-typed `Depends()` parameter is only ever
populated when the connection is an actual `Request` instance, which a
WebSocket connection never is, so the *unmodified* function raised `missing
1 required positional argument: 'request'` under this WebSocket route. The
function body, and every existing HTTP-side call/override, is unchanged.

RMS thresholds (`voice_vad_speech_rms_threshold=0.02`,
`voice_vad_silence_ms=800.0`, `voice_vad_min_speech_ms=200.0`) were chosen
against this project's own fixture, not picked blind: its silent gaps
measure ~0.0 normalized RMS and its in-speech 20ms windows range
~0.04-0.24, so 0.02 sits comfortably above the noise floor and well below
typical speech energy. There is no universally "correct" threshold — this
is a starting point to tune against real usage, unlike `voice_stt_model`'s
measured choice.

New tests: `backend/tests/integration/test_voice_websocket.py`, 5 tests —
a full utterance streamed in chunks through the real Jev fast path to a
real `CommandRunner` result; near-silence never triggering `speech_started`
(asserted indirectly via DB absence, described in the Testing section
below); the max-duration cutoff completing normally, not erroring; a
mid-utterance drop recording exactly one `voice_connection_dropped` event;
and an STT failure returning `error: stt_failed` while the connection
survives to handle a second utterance. Full suite: 223 passed (218 + 5), 6
skipped, ruff clean. No live LLM/Jev API call and no Alembic/schema command
were used to verify any of it — Jev is mocked via the existing
`fake_typesafe`/`jev_settings`/`jev_response` fixtures, and STT is real
local `faster-whisper` only where Phase 2's precedent already established
that as free and deterministic-enough (the full-utterance test); the
VAD-focused tests mock `transcribe_array` or use synthetic audio instead of
depending on real STT output content.

### Phase 4 — Frontend mic control ✅
A mic button in `ChatPanel`'s input row opens the `/api/voice/{session_id}`
WebSocket (`src/hooks/useVoiceInput.ts`), streams captured mic audio
(`src/audio/pcmCapture.ts`), and renders the resulting turn through the
exact same message-list/badge rendering a typed turn uses — no new visual
language, no new badge component.

`useVoiceInput` owns only the WebSocket connection and audio capture;
`ChatPanel` keeps owning the message list and the "first message of a new
session" bookkeeping, wiring the two together via three callbacks
(`onTranscript`, `onChatResponse`, `onError`) — the same split `useTrace`
already established for trace polling. Session id: the WebSocket path
requires one up front (unlike `POST /api/chat`, which can assign one), so
the frontend generates it client-side (`crypto.randomUUID()`) when there
isn't one yet and applies it the moment the response confirms it, mirroring
`handleSubmit`'s existing new-session bookkeeping with the transcript as
the preview text in place of the typed message.

**Audio capture**: `ScriptProcessorNode` (not `AudioWorkletNode` — deprecated
but simpler, and this project already prefers simple-and-sufficient over the
more complex correct-successor where the difference doesn't matter at this
scale, the same tradeoff as energy-based VAD over Silero on the backend),
routed through a zero-gain node into the destination so it fires without
audible echo.

**Downsampling — corrected during review, not shipped as first written**:
initial nearest-integer-ratio decimation (`keep every Nth sample, N =
round(sourceRate/16000)`) only produces genuinely-16kHz audio when the
browser's native rate happens to be an exact multiple of 16000 (48000,
96000). At the other extremely common native rate, 44100Hz, that ratio
rounds to 3, silently producing audio at an *effective* 14700Hz mislabeled
as 16000Hz — an 8.8% speed/pitch distortion fed straight into Whisper,
confirmed by computing the actual effective rate for common browser sample
rates rather than assumed. Replaced with continuous-phase linear
interpolation resampling (a running fractional source index, carried across
`onaudioprocess` chunk boundaries so the phase doesn't reset and drift every
~4096 samples) — still simple, but correct for any source rate, not only
exact multiples of 16000.

Full teardown (processor/source/gain disconnected, every `MediaStreamTrack`
stopped, `AudioContext` closed, WebSocket closed) runs on explicit stop,
component unmount, and an unrecoverable capture error alike, so the
browser's mic-in-use indicator never stays lit after the user is done.

**Errors while voice is active**: a server `{"type":"error"}` event (e.g.
`empty_transcript`) does not tear down the connection — the backend
contract already returns to `ready` and keeps listening, so recording
continues and the error is just surfaced, rather than forcing the user to
click the mic again for a transient per-turn failure. A `startCapture()`
returning `null` (no `getUserMedia`/`AudioContext` in this browser, or an
insecure context blocking mic access) is treated as a real error and tears
the connection down — the alternative, silently sitting at "Listening..."
forever with no audio ever sent, was worse than a clear message.

New tests: `ChatPanel.test.tsx`, 4 — a full transcript→chat_response turn
renders user-then-assistant messages with the correct badge, in the
verified order (checked via bubble container `textContent`, not
`getByText`, since the assistant bubble renders through `ReactMarkdown`'s
nested `<p>` while the user bubble is plain text — an asymmetry that made a
single selector-scoped text query unreliable); a server error surfaces
without losing prior chat history; the mic button and typed-input/Send are
mutually disabled while the other input method is active; unmount closes
the WebSocket. A small controllable `FakeWebSocket` test double (same
spirit as this file's existing `createFetchMock`) makes this possible with
no real microphone — `getUserMedia`/`AudioContext` don't exist in jsdom,
but `pcmCapture.ts` feature-detects and no-ops cleanly, and none of these
tests trigger real capture regardless. Full suite: 33 passed, ruff/build/
lint all clean.

**Not verified, and can't be from this environment**: real browser mic
permission prompts, actual audio quality end-to-end against the live
backend, and that clicking "stop" genuinely clears the browser's
mic-in-use indicator — all real-hardware/real-browser behavior outside
what a headless review or a jsdom test can exercise. Needs a manual check
in an actual browser against a running backend before this is trusted
beyond "the code is correct by inspection and passes what can be
automated."

## Testing

Two idioms now exist in `backend/tests/`:
- **Real local STT, mocked everything downstream** (Phase 2, established):
  `faster-whisper` runs for real (no cost, no network after the first
  model download) against a checked-in fixture audio file; Jev and the
  agent model are mocked exactly like every other test in this project,
  per the live-API-minimization policy.
- **WebSocket testing** (Phase 3, established):
  `starlette.testclient.TestClient.websocket_connect()` — a new idiom for
  this repo; nothing else in `backend/tests/` tests a WebSocket endpoint.
  `test_voice_websocket.py` builds its WS test audio by reading the
  checked-in fixture WAV's raw PCM via Python's `wave` module and sending
  it to the test client across several chunks, simulating streaming,
  rather than adding a new fixture.

  This idiom needed its own DB plumbing, deliberately different from every
  other integration test's `db_session`/`seeded_session`/
  `agent_session_factory` fixtures, for a concrete, verified reason:
  `TestClient.websocket_connect()` runs the ASGI app inside its own
  background thread with its own event loop (an anyio "blocking portal").
  An async SQLAlchemy engine created inside pytest-asyncio's loop (what
  every other fixture does) and then used from inside that portal thread
  fails every time with asyncpg's "attached to a different loop" error —
  reproduced directly against this project's own Postgres while building
  this file, not assumed from documentation. `test_voice_websocket.py`
  therefore uses plain sync `def test_...` functions (pytest-asyncio only
  manages `async def` tests), never `with TestClient(app) as client:`
  (which would also run `app.main`'s real `lifespan`, building a real
  LangGraph checkpointer against the *dev* database — avoided entirely),
  and overrides `get_session_factory` with a callable that builds its
  engine lazily, on first actual use from inside the portal thread.

  A second harness quirk, also verified rather than assumed:
  `WebSocketTestSession.__exit__` sends the client-side disconnect and then
  cancels the portal's task shortly after, without waiting for the
  server's disconnect-handling coroutine to finish — so the
  dropped-connection test calls `ws.close()` itself and polls briefly for
  the resulting DB row *before* letting the `with` block exit, rather than
  relying on that automatic cleanup for timing.

  Because these tests' DB writes are genuinely committed (not the
  rolled-back-per-test transaction every other integration test uses),
  `test_voice_websocket.py` also cleans up its own `AgentEvent`/
  `ConversationMessage`/`AgentSession` rows per test (`voice_session_id`
  fixture) — otherwise a committed `jev_invoked` row would permanently
  leak into `test_jev_fast_path_api.py`'s unscoped cost-comparison
  assertions, which happened once while building this file before that
  cleanup existed.

## Configuration

```dotenv
VOICE_STT_MODEL=tiny.en
VOICE_MAX_UTTERANCE_SECONDS=15.0
VOICE_VAD_SPEECH_RMS_THRESHOLD=0.02
VOICE_VAD_SILENCE_MS=800.0
VOICE_VAD_MIN_SPEECH_MS=200.0
```

No new secret or paid API for this phase — local STT only.

## Source Map

| Concern | File |
|---|---|
| Shared routing implementation | `backend/app/api/routes/chat.py` (`handle_chat_message`) |
| Local speech-to-text | `backend/app/voice/stt.py` (`transcribe`, `transcribe_array`) |
| Energy-based VAD state machine | `backend/app/voice/vad.py` (`UtteranceDetector`) |
| Settings | `backend/app/config.py` (`voice_stt_model`, `voice_max_utterance_seconds`, `voice_vad_speech_rms_threshold`, `voice_vad_silence_ms`, `voice_vad_min_speech_ms`) |
| Fixed-file pipeline proof | `backend/tests/integration/test_voice_stt_pipeline.py` |
| Test fixture audio | `backend/tests/fixtures/audio/list_delayed_mri_appointments.wav` |
| WebSocket endpoint | `backend/app/api/routes/voice.py` |
| WebSocket tests | `backend/tests/integration/test_voice_websocket.py` |
| Frontend mic control | `frontend/src/hooks/useVoiceInput.ts`, `frontend/src/audio/pcmCapture.ts`, `frontend/src/components/ChatPanel.tsx` |

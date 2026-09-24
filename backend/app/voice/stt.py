"""Local speech-to-text via `faster-whisper` (experimental, branch `voice`).

Why local, not a hosted streaming API: both were cheap enough at this
project's scale that cost wasn't the deciding factor (see docs/voice.md's
full comparison — a hosted option ran a fraction of a cent per command).
The deciding factor was consistency with a property this project already
has: it runs offline except for the live LLM/Jev calls it deliberately
makes. A hosted STT vendor would be a third metered dependency alongside
Anthropic and TypeSafe; local Whisper keeps that property intact through
the audio layer too.

Why `faster-whisper` specifically: its base install pulls in `ctranslate2`,
`huggingface-hub`, `tokenizers`, `onnxruntime`, `av`, `tqdm` — verified
against its own package metadata, not assumed — and no `torch`. That
matters because nothing else in this stack touches torch; a heavier choice
here would be a real weight increase for the whole project, not just this
feature.

Why "tiny.en": measured directly against this project's own test fixture
(`tests/fixtures/audio/list_delayed_mri_appointments.wav`, a synthesized
"list delayed MRI appointments") rather than assumed from the model card —
~450ms average transcription on CPU once the model is warm, well inside
the "a few hundred ms to ~1-2s" target for a responsive command interface.
`base.en` was also measured (~600-800ms) and was not meaningfully more
accurate on short, clear command audio; `tiny.en` is the better fit for
this project's actual utterances, which are short imperative commands, not
long-form dictation.

The model is loaded once per process, not per request — loading it is the
slow part (roughly 1s warm, longer on first-ever download); transcription
itself is fast once it's resident. This mirrors how `app/main.py`'s
`lifespan` builds the LangGraph checkpointer once for the process, not per
request.

What calls it: `app/api/routes/voice.py` (Phase 3, per-utterance, after VAD
has decided an utterance ended) and the Phase 2 fixed-file proof in
`tests/integration/test_voice_stt_pipeline.py`.
"""

import asyncio
from functools import lru_cache

import numpy as np


@lru_cache
def _load_model(model_size: str):
    """Cached per model name (there's only ever one in practice —
    `settings.voice_stt_model` — but caching by name rather than a bare
    singleton keeps this honest about what it's actually keyed on, and lets
    a test load a different size without fighting a global). CPU + int8:
    this project has no GPU dependency anywhere else and command-length
    audio doesn't need one.

    Imported here, not at module level: `app/main.py` imports the voice route
    at startup, and importing faster-whisper (ctranslate2, onnxruntime) there
    would cost startup time and memory on every deployment, including ones
    that never receive a voice utterance."""
    from faster_whisper import WhisperModel

    return WhisperModel(model_size, device="cpu", compute_type="int8")


def _transcribe_sync(model_size: str, audio_path: str) -> str:
    model = _load_model(model_size)
    segments, _info = model.transcribe(audio_path, beam_size=5)
    # transcribe() returns a lazy generator of segments; joining here (not
    # in the caller) keeps this function's return type a plain str, so
    # nothing downstream needs to know faster-whisper's segment shape.
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe(audio_path: str, model_size: str) -> str:
    """Transcribes a WAV file to text. Runs the blocking `faster-whisper`
    call in a thread so it doesn't block the event loop — the same pattern
    `app/agent/jev_fast_path.py` uses for its own blocking SDK call, since
    this function is meant to be awaited from inside an async FastAPI
    request/WebSocket handler."""
    return await asyncio.to_thread(_transcribe_sync, model_size, audio_path)


def _transcribe_array_sync(model_size: str, samples: np.ndarray) -> str:
    model = _load_model(model_size)
    # faster-whisper accepts a float32 numpy array directly (no temp file
    # needed) -- exactly what app/api/routes/voice.py's VAD hands over once
    # an utterance is finalized. Same beam_size as the file-based path, same
    # lazy-generator-joining rationale (see _transcribe_sync above).
    segments, _info = model.transcribe(samples, beam_size=5)
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe_array(samples: np.ndarray, model_size: str) -> str:
    """Sibling to `transcribe()` for an in-memory utterance instead of a WAV
    file on disk: `samples` is float32 PCM normalized to [-1, 1] (divide by
    32768.0), the VAD-finalized buffer from `app/api/routes/voice.py`.
    Reuses the same cached `_load_model` loader and the same
    thread-offload pattern as `transcribe()` -- does not touch `transcribe`
    or `_transcribe_sync` at all, so Phase 2's file-based test keeps
    exercising exactly the code path it always has."""
    return await asyncio.to_thread(_transcribe_array_sync, model_size, samples)

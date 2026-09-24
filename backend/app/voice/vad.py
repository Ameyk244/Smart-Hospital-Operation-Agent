"""Energy-based (RMS) voice-activity detection (Phase 3, branch `voice`).

Why RMS, not Silero: see docs/voice.md's "VAD: energy-based (RMS), not
Silero" section -- Silero's packaged install pulls in torch/torchaudio, a
genuinely heavy addition nothing else in this stack needs. A simple
loudness threshold is enough to bound utterance start/stop for short
command-length audio in a reasonably quiet environment, this project's
actual use case.

Design: chunk-level, not sample-level. The client sends arbitrary-sized
binary WS frames (see app/api/routes/voice.py's audio contract) with no
timing guarantee, so this detector measures RMS once per received chunk and
tracks state transitions in terms of *audio duration* (derived from sample
count / 16000 Hz), never wall-clock time. That matters for testability: a
test can feed chunks as fast as it wants (no real-time pacing) and still
get deterministic transitions, because "200ms of speech" means 200ms worth
of PCM samples, not 200ms of wall time between calls.

State machine (mirrors the brief in docs/voice.md/the Phase 3 build):
  IDLE   -- below threshold, buffering only a *candidate* run (not yet
            confirmed as real speech). Recent audio that did not make it into
            a confirmed utterance is kept in a short pre-roll and prepended
            when speech is confirmed, so a soft word onset is not lost.
  SPEECH -- confirmed speech in progress; every chunk is appended to the
            utterance buffer regardless of whether that specific chunk is
            above or below threshold (silence right after the last word is
            still part of the utterance, per the max_duration/silence
            wording in the brief); a silence run below threshold resets on
            every above-threshold chunk.

What calls it: app/api/routes/voice.py, one `UtteranceDetector` per
WebSocket connection (not per utterance -- `ingest()` returns to IDLE on
its own after finalizing, ready for the connection's next turn).
"""

from collections import deque
from dataclasses import dataclass
from enum import Enum

import numpy as np

# Fixed by the audio contract (app/api/routes/voice.py): 16-bit signed PCM,
# mono, little-endian, 16000 Hz. Not configurable -- a different sample rate
# would need a different WS contract, not a setting.
SAMPLE_RATE_HZ = 16_000
BYTES_PER_SAMPLE = 2


class UtteranceEndReason(str, Enum):
    SILENCE = "silence"
    MAX_DURATION = "max_duration"


@dataclass(frozen=True)
class SpeechStarted:
    """Emitted once, the moment RMS has stayed above threshold for at least
    `voice_vad_min_speech_ms`."""


@dataclass(frozen=True)
class SpeechEnded:
    """Emitted once per finalized utterance. `audio` is the raw int16 PCM
    bytes accumulated since `SpeechStarted` (inclusive of the candidate run
    that led to confirmation, and inclusive of any trailing silence that
    triggered the `SILENCE` reason) -- the caller converts it to a
    normalized float32 array before handing it to `transcribe_array`."""

    reason: UtteranceEndReason
    audio: bytes


def _chunk_duration_ms(num_samples: int) -> float:
    return (num_samples / SAMPLE_RATE_HZ) * 1000.0


def _chunk_rms(samples: np.ndarray) -> float:
    """RMS of already-normalized ([-1, 1]) float32 samples. Empty input has
    no energy, not undefined energy -- returns 0.0 rather than NaN so a
    zero-length chunk can never spuriously look like speech."""
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples))))


class UtteranceDetector:
    """One instance per WebSocket connection. Not thread-safe and not meant
    to be -- a single connection is handled by a single coroutine, the same
    assumption the rest of app/api/routes/voice.py makes.

    Handles the arbitrary-chunk-boundary edge case the raw-PCM-over-WS
    contract implies: a 16-bit sample can, in principle, straddle two
    binary frames. A single held-over byte is carried across `ingest()`
    calls so every chunk this detector actually measures RMS on and
    appends to a buffer is a whole number of samples.
    """

    def __init__(
        self,
        *,
        speech_rms_threshold: float,
        silence_ms: float,
        min_speech_ms: float,
        max_utterance_ms: float,
        preroll_ms: float = 0.0,
    ) -> None:
        self._speech_rms_threshold = speech_rms_threshold
        self._silence_ms = silence_ms
        self._min_speech_ms = min_speech_ms
        self._max_utterance_ms = max_utterance_ms
        self._preroll_ms_limit = preroll_ms

        self._in_speech = False
        self._leftover_byte = b""

        # IDLE-state candidate run: audio accumulated since RMS first went
        # above threshold, not yet confirmed as a real utterance.
        self._candidate_bytes = bytearray()
        self._candidate_ms = 0.0

        # Most recent chunks that were *not* part of a confirmed utterance
        # (silence, and candidate runs that broke off), oldest first, capped
        # at `preroll_ms` of audio: (bytes, duration_ms).
        self._preroll: deque[tuple[bytes, float]] = deque()
        self._preroll_ms = 0.0

        # SPEECH-state confirmed utterance.
        self._utterance_bytes = bytearray()
        self._utterance_ms = 0.0
        self._silence_run_ms = 0.0

    def is_in_speech(self) -> bool:
        return self._in_speech

    def buffered_ms(self) -> float:
        """How much confirmed-utterance audio is currently buffered. 0 when
        not in SPEECH state. Used only to report how much was discarded on
        a mid-utterance connection drop -- see app/api/routes/voice.py's
        VOICE_DROPPED_EVENT_TYPE."""
        return self._utterance_ms if self._in_speech else 0.0

    def ingest(self, chunk: bytes) -> SpeechStarted | SpeechEnded | None:
        """Feed one received WS binary frame. Returns at most one event per
        call: a chunk can confirm speech-start or trigger finalization, but
        never both in the same call (finalization only happens once already
        in SPEECH state)."""
        data = self._leftover_byte + chunk
        if len(data) % BYTES_PER_SAMPLE:
            self._leftover_byte = data[-1:]
            data = data[:-1]
        else:
            self._leftover_byte = b""
        if not data:
            return None

        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        rms = _chunk_rms(samples)
        duration_ms = _chunk_duration_ms(len(samples))
        is_speech_chunk = rms >= self._speech_rms_threshold

        if not self._in_speech:
            return self._ingest_idle(data, duration_ms, is_speech_chunk)
        return self._ingest_speech(data, duration_ms, is_speech_chunk)

    def _ingest_idle(
        self, data: bytes, duration_ms: float, is_speech_chunk: bool
    ) -> SpeechStarted | None:
        if not is_speech_chunk:
            # A single below-threshold chunk breaks the candidate run. This
            # is intentionally strict (no leniency/hangover here) -- the
            # *speech* short-run-tolerance the min_speech_ms window already
            # exists to catch is a click/pop, not a run that's already
            # intermittent below threshold.
            # The run is not thrown away: it goes to the pre-roll, so if
            # speech is confirmed a moment later the broken-off onset is
            # still part of the utterance.
            if self._candidate_bytes:
                self._push_preroll(bytes(self._candidate_bytes), self._candidate_ms)
            self._candidate_bytes.clear()
            self._candidate_ms = 0.0
            self._push_preroll(data, duration_ms)
            return None

        self._candidate_bytes += data
        self._candidate_ms += duration_ms
        if self._candidate_ms < self._min_speech_ms:
            return None

        # Confirmed: the candidate run becomes the start of the real
        # utterance buffer.
        self._in_speech = True
        preroll = b"".join(chunk for chunk, _ms in self._preroll)
        self._utterance_bytes = bytearray(preroll) + self._candidate_bytes
        self._utterance_ms = self._preroll_ms + self._candidate_ms
        self._preroll.clear()
        self._preroll_ms = 0.0
        self._silence_run_ms = 0.0
        self._candidate_bytes = bytearray()
        self._candidate_ms = 0.0
        return SpeechStarted()

    def _push_preroll(self, data: bytes, duration_ms: float) -> None:
        if self._preroll_ms_limit <= 0:
            return
        self._preroll.append((data, duration_ms))
        self._preroll_ms += duration_ms
        # Drop whole oldest chunks while the remainder still covers the limit;
        # chunk-granular on purpose (no sample slicing), so the pre-roll can
        # run at most one chunk over the limit.
        while len(self._preroll) > 1 and self._preroll_ms - self._preroll[0][1] >= self._preroll_ms_limit:
            _chunk, ms = self._preroll.popleft()
            self._preroll_ms -= ms

    def _ingest_speech(
        self, data: bytes, duration_ms: float, is_speech_chunk: bool
    ) -> SpeechEnded | None:
        self._utterance_bytes += data
        self._utterance_ms += duration_ms
        if is_speech_chunk:
            self._silence_run_ms = 0.0
        else:
            self._silence_run_ms += duration_ms

        # Max-duration cutoff checked first: it is a successful completion
        # forced by duration, not a failure, and takes priority over a
        # silence run that happens to complete on the same chunk.
        if self._utterance_ms >= self._max_utterance_ms:
            return self._finalize(UtteranceEndReason.MAX_DURATION)
        if self._silence_run_ms >= self._silence_ms:
            return self._finalize(UtteranceEndReason.SILENCE)
        return None

    def _finalize(self, reason: UtteranceEndReason) -> SpeechEnded:
        audio = bytes(self._utterance_bytes)
        self._in_speech = False
        self._utterance_bytes = bytearray()
        self._utterance_ms = 0.0
        self._silence_run_ms = 0.0
        return SpeechEnded(reason=reason, audio=audio)

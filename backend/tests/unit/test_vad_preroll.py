"""The VAD pre-roll: audio just before speech is confirmed stays in the
utterance. Found in a spoken test: "what preferences have you saved" reached
the domain gate as "preferences have you saved?" and was rejected, because the
word onset sat below the RMS threshold for a chunk and the run that broke off
was discarded. Pure numpy, no STT, no network.
"""

import numpy as np

from app.voice.vad import SAMPLE_RATE_HZ, SpeechEnded, SpeechStarted, UtteranceDetector

CHUNK_MS = 100
THRESHOLD = 0.02


def _tone(amplitude: float, ms: int = CHUNK_MS) -> bytes:
    """Constant-value chunk: a distinct int16 value per marker so a test can
    tell exactly which chunks ended up in the utterance."""
    n = SAMPLE_RATE_HZ * ms // 1000
    return np.full(n, int(amplitude * 32768), dtype="<i2").tobytes()


def _detector(preroll_ms: float | None) -> UtteranceDetector:
    kwargs = {} if preroll_ms is None else {"preroll_ms": preroll_ms}
    return UtteranceDetector(
        speech_rms_threshold=THRESHOLD,
        silence_ms=800,
        min_speech_ms=200,
        max_utterance_ms=15_000,
        **kwargs,
    )


def _run(detector: UtteranceDetector, chunks: list[bytes]) -> bytes | None:
    for chunk in chunks:
        event = detector.ingest(chunk)
        if isinstance(event, SpeechEnded):
            return event.audio
    return None


def _values(audio: bytes) -> set[int]:
    return {int(v) for v in np.frombuffer(audio, dtype="<i2")}


SOFT = 0.01  # below THRESHOLD: a soft consonant onset
SPEECH = 0.2
QUIET = 0.0
TRAILING = [_tone(QUIET)] * 10  # 1s of silence ends the utterance


def test_soft_onset_just_before_speech_is_kept_with_preroll():
    chunks = [_tone(QUIET)] * 5 + [_tone(SOFT)] * 2 + [_tone(SPEECH)] * 4 + TRAILING
    audio = _run(_detector(300), chunks)
    assert audio is not None
    assert int(SOFT * 32768) in _values(audio)


def test_soft_onset_is_lost_without_preroll_which_is_the_original_bug():
    chunks = [_tone(QUIET)] * 5 + [_tone(SOFT)] * 2 + [_tone(SPEECH)] * 4 + TRAILING
    audio = _run(_detector(0), chunks)
    assert audio is not None
    assert int(SOFT * 32768) not in _values(audio)


def test_default_is_no_preroll_so_existing_callers_are_unchanged():
    chunks = [_tone(QUIET)] * 5 + [_tone(SOFT)] * 2 + [_tone(SPEECH)] * 4 + TRAILING
    audio = _run(_detector(None), chunks)
    assert audio is not None
    assert int(SOFT * 32768) not in _values(audio)


def test_a_broken_off_loud_run_is_recovered_not_discarded():
    """One loud chunk, a dip below threshold, then sustained speech: the first
    loud chunk is real speech and used to be thrown away when the run broke."""
    first, dip = 0.3, 0.0
    chunks = [_tone(first), _tone(dip)] + [_tone(SPEECH)] * 4 + TRAILING
    audio = _run(_detector(300), chunks)
    assert audio is not None
    assert int(first * 32768) in _values(audio)
    assert int(first * 32768) not in _values(_run(_detector(0), chunks))


def test_preroll_is_bounded_not_the_whole_silence_before_speech():
    chunks = [_tone(QUIET)] * 30 + [_tone(SPEECH)] * 4 + TRAILING  # 3s of silence first
    with_preroll = _run(_detector(300), chunks)
    without = _run(_detector(0), chunks)
    extra_ms = (len(with_preroll) - len(without)) / 2 / SAMPLE_RATE_HZ * 1000
    # 300ms, allowing the documented one-chunk overshoot.
    assert 0 < extra_ms <= 300 + CHUNK_MS


def test_speech_started_still_needs_min_speech_of_real_speech():
    """Pre-roll changes what is kept, never when speech is declared: quiet
    audio alone must not start an utterance."""
    detector = _detector(300)
    events = [detector.ingest(_tone(SOFT)) for _ in range(20)]
    assert not any(isinstance(e, SpeechStarted) for e in events)


def test_a_second_utterance_does_not_inherit_the_first_ones_preroll():
    detector = _detector(300)
    first = _run(detector, [_tone(0.005)] * 3 + [_tone(SPEECH)] * 4 + TRAILING)
    assert first is not None
    second = _run(detector, [_tone(QUIET)] * 5 + [_tone(0.3)] * 4 + TRAILING)
    assert second is not None
    assert int(SPEECH * 32768) not in _values(second)

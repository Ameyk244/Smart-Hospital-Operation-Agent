// Browser-side capture pipeline for the voice input feature (see
// src/hooks/useVoiceInput.ts, which owns the WebSocket lifecycle this
// plugs into). Pulls raw microphone samples via the Web Audio API,
// downsamples them to the 16kHz mono 16-bit signed little-endian PCM the
// backend's /api/voice/{session_id} WebSocket requires with no
// negotiation, and sends each chunk as a binary frame as it's produced.
//
// Uses ScriptProcessorNode rather than AudioWorkletNode. ScriptProcessorNode
// is deprecated in favor of AudioWorklet, but AudioWorklet requires
// shipping and registering a separate worklet module file with its own
// bundling/MIME-type path, and this project already prefers the simpler
// option where it's good enough for its scope (see the backend's
// energy-based VAD for the same "simple beats heavy, at this scale"
// tradeoff, per JEV.md/rulebook.md). ScriptProcessorNode's main-thread
// cost and latency are a non-issue at this scale (one active recording at
// a time, short utterances).
//
// Downsampling is linear-interpolation resampling with a phase carried
// across chunk boundaries, not a proper windowed-sinc filter — still
// deliberately simple, per the task brief, but *not* nearest-integer-ratio
// decimation: that approach (keep every Nth sample, N = round(rate/16000))
// only produces genuinely 16kHz audio when the source rate is an exact
// multiple of 16000 (48000, 96000 — both real and common). At the other
// extremely common native rate, 44100Hz, round(44100/16000) = 3, which
// decimates to an *effective* 14700Hz — every sample is silently
// mislabeled as 1/16000s when it actually represents 1/14700s, an 8.8%
// speed/pitch distortion fed straight into Whisper. Verified by computing
// the actual effective rate for common browser sample rates before
// shipping this, not assumed. Linear interpolation at a continuous
// fractional step (source_rate / 16000) has no such gap: it always
// produces audio genuinely at 16000Hz, for any source rate.

const TARGET_SAMPLE_RATE = 16000;
// Largest standard ScriptProcessorNode buffer size: keeps the number of
// onaudioprocess callbacks (and therefore WS sends) low without adding
// noticeable latency for a chat-turn-scale utterance.
const BUFFER_SIZE = 4096;

export interface CaptureHandle {
  stop: () => void;
}

/**
 * Starts capturing the microphone and streaming downsampled PCM frames to
 * `ws` as binary WebSocket messages. Resolves to a handle whose `stop()`
 * tears down every piece (processor, source, tracks, AudioContext) — see
 * this module's docstring for why each of those matters — or to `null` if
 * this environment has no microphone support to speak of.
 *
 * The `navigator.mediaDevices.getUserMedia` feature-detection below isn't
 * just defensive coding: it's also what lets a test exercise the rest of
 * the voice WebSocket's event handling (transcript/chat_response/error)
 * by driving a fake WebSocket directly, without a real microphone or
 * jsdom needing to implement any Web Audio API at all — see
 * useVoiceInput.ts's docstring and ChatPanel.test.tsx's voice tests.
 */
export async function startCapture(ws: WebSocket): Promise<CaptureHandle | null> {
  if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
    return null;
  }

  const AudioContextCtor =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextCtor) {
    return null;
  }

  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const audioContext = new AudioContextCtor();
  const source = audioContext.createMediaStreamSource(stream);
  // createScriptProcessor is deprecated (see module docstring for why it's
  // still used here); not disabled via a lint comment since this project's
  // oxlint config doesn't enable a deprecation rule to begin with.
  const processor = audioContext.createScriptProcessor(BUFFER_SIZE, 1, 1);
  // How far to advance the (fractional) source index per output sample.
  // 1.0 when the context is already at 16kHz -- no resampling needed.
  const step = audioContext.sampleRate / TARGET_SAMPLE_RATE;
  // Fractional position within the *current* chunk of the next sample to
  // produce, carried across onaudioprocess calls so the resampling phase
  // stays continuous instead of restarting (and drifting) every chunk.
  let phase = 0;

  processor.onaudioprocess = (event: AudioProcessingEvent) => {
    if (ws.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    const outSamples: number[] = [];
    let pos = phase;
    while (pos < input.length) {
      const i0 = Math.floor(pos);
      // Clamp the "next" neighbor to the last real sample rather than
      // reading past the chunk -- the tiny resulting flat-interpolation at
      // each ~4096-sample chunk boundary is inaudible for speech-length
      // audio and far preferable to indexing undefined/out-of-range data.
      const i1 = Math.min(i0 + 1, input.length - 1);
      const frac = pos - i0;
      const sample = input[i0] * (1 - frac) + input[i1] * frac;
      outSamples.push(Math.max(-32768, Math.min(32767, Math.round(sample * 32768))));
      pos += step;
    }
    // Carry the overshoot into the next chunk so consecutive chunks form
    // one continuous resampled stream, not `outSamples.length` independent
    // ones each restarting at phase 0.
    phase = pos - input.length;
    if (outSamples.length === 0) return;
    ws.send(new Int16Array(outSamples).buffer);
  };

  // A ScriptProcessorNode only fires onaudioprocess while it's connected
  // into a live graph that reaches the destination. Route it through a
  // zero-gain node so captured audio never actually plays back (no echo)
  // while still driving the callback above.
  const silentGain = audioContext.createGain();
  silentGain.gain.value = 0;
  source.connect(processor);
  processor.connect(silentGain);
  silentGain.connect(audioContext.destination);

  return {
    stop: () => {
      processor.onaudioprocess = null;
      try {
        source.disconnect();
        processor.disconnect();
        silentGain.disconnect();
      } catch {
        // Already disconnected — nothing to clean up.
      }
      // Required, not optional: without this the browser's mic-in-use
      // indicator stays lit after the user clicks "stop" (see task brief).
      stream.getTracks().forEach((track) => track.stop());
      if (audioContext.state !== "closed") {
        void audioContext.close();
      }
    },
  };
}

import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE_URL } from "../api/client";
import type { ChatResponse } from "../api/types";
import { startCapture } from "../audio/pcmCapture";

// Owns the /api/voice/{session_id} WebSocket connection: opening it,
// streaming captured microphone audio into it (via pcmCapture.ts), parsing
// the server's JSON event frames, and tearing everything down cleanly.
// Deliberately does NOT own the chat message list / session bookkeeping —
// ChatPanel already has that state and the exact "first message of a new
// session" logic handleSubmit uses, so this hook just reports raw events
// up via callbacks and lets ChatPanel wire them in the same way it wires
// handleSubmit's results. See useTrace.ts for this project's other
// extracted-hook-around-one-concern precedent.

export interface VoiceInputCallbacks {
  onTranscript: (text: string) => void;
  onChatResponse: (response: ChatResponse) => void;
  onError: (message: string) => void;
}

export interface VoiceInputState {
  // True from the moment the mic button starts a session until it's fully
  // torn down (explicit stop, unmount, or an unrecoverable connection
  // failure) — not just "currently capturing audio".
  active: boolean;
  // Short human status line for the same "assistant is working" slot
  // ChatPanel already renders sending/liveStatus through — see
  // ChatPanel.tsx's pending-message block.
  status: string | null;
  start: (sessionId: string) => void;
  stop: () => void;
}

interface VoiceServerEvent {
  type: string;
  text?: string;
  reason?: string;
  response?: ChatResponse;
  code?: string;
  message?: string;
}

function voiceWsUrl(sessionId: string): string {
  const scheme = API_BASE_URL.startsWith("https") ? "wss" : "ws";
  const hostAndPath = API_BASE_URL.replace(/^https?:\/\//, "");
  return `${scheme}://${hostAndPath}/api/voice/${encodeURIComponent(sessionId)}`;
}

export function useVoiceInput({ onTranscript, onChatResponse, onError }: VoiceInputCallbacks): VoiceInputState {
  const [active, setActive] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const captureRef = useRef<{ stop: () => void } | null>(null);

  // Latest callbacks, read from inside the WebSocket's long-lived
  // onmessage handler instead of closing over the values passed into
  // start() at connect time — a single connection spans many turns (per
  // the backend contract), and ChatPanel's onChatResponse closure changes
  // every render (it reads the current sessionId prop), so a plain
  // closure captured once at connect time would go stale after the very
  // first turn (e.g. re-running "brand new session" bookkeeping forever).
  const onTranscriptRef = useRef(onTranscript);
  const onChatResponseRef = useRef(onChatResponse);
  const onErrorRef = useRef(onError);
  useEffect(() => {
    onTranscriptRef.current = onTranscript;
  }, [onTranscript]);
  useEffect(() => {
    onChatResponseRef.current = onChatResponse;
  }, [onChatResponse]);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  const teardown = useCallback(() => {
    const ws = wsRef.current;
    wsRef.current = null;
    captureRef.current?.stop();
    captureRef.current = null;
    if (ws) {
      // Detach handlers before closing so this ws's own onclose (which
      // also calls teardown, for the unprompted-server-close case) can't
      // re-enter an already-finished teardown.
      ws.onclose = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.close();
    }
    setActive(false);
    setStatus(null);
  }, []);

  // Unmount safety net: a user navigating away or the tab closing must not
  // leave the browser's mic-in-use indicator lit — see pcmCapture.ts's
  // stop() for what actually releases the hardware.
  useEffect(() => {
    return () => teardown();
  }, [teardown]);

  const start = useCallback(
    (sessionId: string) => {
      if (wsRef.current) return; // a turn is already active on this connection
      setActive(true);
      setStatus("Connecting…");

      const ws = new WebSocket(voiceWsUrl(sessionId));
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        startCapture(ws)
          .then((handle) => {
            if (handle === null) {
              // Feature-detection failure (no getUserMedia/AudioContext in
              // this browser, or an insecure context blocking mic access).
              // Without this, the UI would sit at "Listening..." forever
              // with no audio ever sent and no indication anything is
              // wrong -- worse than a clear error.
              onErrorRef.current("Voice input isn't supported in this browser.");
              teardown();
              return;
            }
            captureRef.current = handle;
          })
          .catch((err: unknown) => {
            onErrorRef.current(
              `Microphone access failed: ${err instanceof Error ? err.message : String(err)}`,
            );
            teardown();
          });
      };

      ws.onmessage = (event: MessageEvent) => {
        if (typeof event.data !== "string") return; // server only ever sends JSON text frames
        let parsed: VoiceServerEvent;
        try {
          parsed = JSON.parse(event.data) as VoiceServerEvent;
        } catch {
          return;
        }
        switch (parsed.type) {
          case "ready":
            setStatus("Listening…");
            break;
          case "speech_started":
            setStatus("Listening… (speech detected)");
            break;
          case "speech_ended":
          case "transcribing":
            setStatus("Transcribing…");
            break;
          case "transcript":
            if (parsed.text) onTranscriptRef.current(parsed.text);
            break;
          case "chat_response":
            if (parsed.response) onChatResponseRef.current(parsed.response);
            setStatus("Listening…");
            break;
          case "error":
            // The connection stays open and the server returns to "ready"
            // per the contract, so we deliberately keep recording through
            // a turn-level error (e.g. empty_transcript) instead of
            // forcing the user to click the mic again — surfacing it via
            // onError is enough for them to see what happened.
            onErrorRef.current(parsed.message ?? "Voice error.");
            setStatus("Listening…");
            break;
          default:
            break;
        }
      };

      ws.onerror = () => {
        onErrorRef.current("Voice connection error.");
      };

      ws.onclose = () => {
        teardown();
      };
    },
    [teardown],
  );

  const stop = useCallback(() => {
    teardown();
  }, [teardown]);

  return { active, status, start, stop };
}

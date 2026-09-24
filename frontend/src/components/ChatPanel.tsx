import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api/client";
import type { AgentEvent, ChatResponse, HandledBy } from "../api/types";
import type { TraceState } from "../hooks/useTrace";
import { useVoiceInput } from "../hooks/useVoiceInput";

// Active-session pointer: sessionStorage (not localStorage) on purpose —
// this is what makes "reload keeps the chat, restart starts fresh" work
// with no custom same-tab-vs-new-tab detection. sessionStorage persists
// across a same-tab reload and is cleared the moment the tab closes.
const ACTIVE_SESSION_KEY = "shoa_session_id";

// The full rendered message list (badges included) for the active session,
// cached alongside the pointer above. /api/sessions/{id}/messages can
// restore *content* after a reload but was never told handled_by, so it
// can't bring badges back — this cache is what lets a same-tab reload look
// pixel-identical, badges included. Also cleared when the tab closes.
const CACHED_MESSAGES_KEY = "shoa_cached_messages";

// Index of past sessions, deliberately in localStorage so it survives a
// full browser restart — this is the "Previous chats" list. Never
// auto-loaded; only ever opened by the explicit control below.
const PREVIOUS_SESSIONS_KEY = "shoa_previous_sessions";

const PREVIEW_MAX_LEN = 80;

interface DisplayMessage {
  key: string;
  role: "user" | "assistant";
  content: string;
  handledBy?: HandledBy;
  terminatedReason?: string | null;
  // Set once an agent-routed turn finishes; renders as a collapsed one-line
  // summary under the badge (see Task D in the brief this file implements).
  progressSummary?: string;
}

interface PreviousSessionEntry {
  session_id: string;
  started_at: string;
  last_message_preview: string;
}

interface CachedMessages {
  sessionId: string;
  messages: DisplayMessage[];
}

const HANDLED_BY_LABEL: Record<HandledBy, string> = {
  deterministic: "deterministic",
  agent: "agent",
  rejected: "rejected",
  jev: "jev",
};

function HandledByBadge({ handledBy }: { handledBy?: HandledBy }) {
  if (!handledBy) return null;
  return <span className={`handled-by-badge handled-by-${handledBy}`}>{HANDLED_BY_LABEL[handledBy]}</span>;
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function readPreviousSessions(): PreviousSessionEntry[] {
  try {
    const raw = localStorage.getItem(PREVIOUS_SESSIONS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as PreviousSessionEntry[]) : [];
  } catch {
    return [];
  }
}

function appendPreviousSession(sessionId: string, firstMessage: string): PreviousSessionEntry[] {
  const list = readPreviousSessions();
  list.unshift({
    session_id: sessionId,
    started_at: new Date().toISOString(),
    last_message_preview: truncate(firstMessage, PREVIEW_MAX_LEN),
  });
  try {
    localStorage.setItem(PREVIOUS_SESSIONS_KEY, JSON.stringify(list));
  } catch {
    // localStorage full/unavailable — the previous-chats index is a
    // nice-to-have, not something worth failing the send over.
  }
  return list;
}

function readCachedMessages(sessionId: string): DisplayMessage[] | null {
  try {
    const raw = sessionStorage.getItem(CACHED_MESSAGES_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedMessages;
    if (parsed.sessionId !== sessionId || !Array.isArray(parsed.messages)) return null;
    return parsed.messages;
  } catch {
    return null;
  }
}

function writeCachedMessages(sessionId: string, messages: DisplayMessage[]): void {
  try {
    sessionStorage.setItem(CACHED_MESSAGES_KEY, JSON.stringify({ sessionId, messages }));
  } catch {
    // sessionStorage full/unavailable — worst case a reload falls back to
    // fetching /messages (without badges) instead of hard-failing here.
  }
}

// Maps a raw AgentEvent (see backend/app/agent/graph.py for the event_type
// values this switches on) to the short human status line shown while the
// agent path is in flight. Kept as a pure function so Task D's tests can
// exercise it directly against fixture events.
function describeTraceEvent(e: AgentEvent): string {
  switch (e.event_type) {
    case "agent_invoked":
      return "Invoking agent…";
    case "tool_requested":
      return e.tool_name ? `Running ${e.tool_name}…` : "Running tool…";
    case "tool_executed":
      return e.tool_name ? `Ran ${e.tool_name}` : "Tool finished";
    // The typed-decision fast path runs *before* the agent, so this is
    // usually the first line a jev-routed turn ever shows. A REJECTED or
    // FAILURE jev event isn't an error for the user — it just means the
    // turn carries on to the agent, so word it as "falling back".
    case "jev_invoked":
      if (e.status === "SUCCESS") {
        return e.tool_name ? `Jev matched ${e.tool_name}…` : "Jev matched a command…";
      }
      if (e.status === "FAILURE") return "Jev unavailable — falling back to the agent…";
      return "Jev wasn't confident — falling back to the agent…";
    case "llm_response":
      return "Thinking…";
    case "llm_timeout":
      return "Retrying…";
    case "max_rounds_exceeded":
      return "Wrapping up…";
    default:
      return "Working…";
  }
}

function summarizeToolCalls(newEvents: AgentEvent[]): string {
  const toolCalls = newEvents.filter((e) => e.event_type === "tool_executed").length;
  return toolCalls > 0 ? `✓ ${toolCalls} tool call${toolCalls === 1 ? "" : "s"}` : "✓ done";
}

interface ChatPanelProps {
  sessionId: string | null;
  onSessionId: (id: string | null) => void;
  onTurnComplete: (touchedEntityCodes: string[]) => void;
  // Compact in-chat progress indicator (Task D) reads from this instead of
  // polling the trace endpoint through a second, independent path.
  trace: TraceState;
}

export function ChatPanel({ sessionId, onSessionId, onTurnComplete, trace }: ChatPanelProps) {
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previousSessions, setPreviousSessions] = useState<PreviousSessionEntry[]>(() =>
    readPreviousSessions(),
  );
  const [liveStatus, setLiveStatus] = useState<string | null>(null);
  const restoredRef = useRef(false);
  const listEndRef = useRef<HTMLDivElement | null>(null);
  // How many trace events already existed for this session before the
  // in-flight turn started — everything after this index in a later
  // /trace fetch belongs to *this* turn, not an earlier one.
  const baselineEventCountRef = useRef(0);

  // Bridges the "transcript" event to the later "chat_response" event for
  // a voice turn — they arrive as two separate server messages, but the
  // brand-new-session bookkeeping below needs the transcript text as its
  // preview, the same way handleSubmit uses the typed text.
  const lastVoiceTranscriptRef = useRef("");

  // Voice input: see useVoiceInput.ts for what it owns (the WebSocket +
  // audio capture) versus what stays here (the same message-list/session
  // bookkeeping handleSubmit already does for typed turns) — these
  // callbacks are the only place that split is bridged.
  const voice = useVoiceInput({
    onTranscript: (text) => {
      lastVoiceTranscriptRef.current = text;
      setMessages((prev) => [...prev, { key: `user-voice-${Date.now()}`, role: "user", content: text }]);
    },
    onChatResponse: (response: ChatResponse) => {
      if (!sessionId) {
        sessionStorage.setItem(ACTIVE_SESSION_KEY, response.session_id);
        onSessionId(response.session_id);
        setPreviousSessions(appendPreviousSession(response.session_id, lastVoiceTranscriptRef.current));
      }
      setMessages((prev) => [
        ...prev,
        {
          key: `assistant-voice-${Date.now()}`,
          role: "assistant",
          content: response.message,
          handledBy: response.handled_by,
          terminatedReason: response.terminated_reason,
        },
      ]);
      onTurnComplete(response.touched_entity_codes);
    },
    onError: (message) => {
      setError(message);
    },
  });

  function handleStartVoice() {
    if (sending || voice.active) return;
    setError(null);
    lastVoiceTranscriptRef.current = "";
    // The WebSocket path requires session_id as a URL segment up front —
    // unlike POST /api/chat, the backend can't assign one for us on a
    // brand-new session, so we generate it client-side. See the task
    // brief: the backend contract treats this exactly like handleSubmit's
    // "first message of a new session" case once the response comes back.
    voice.start(sessionId ?? crypto.randomUUID());
  }

  // On mount: sessionStorage only holds a session_id if this is the same
  // tab reloading (see ACTIVE_SESSION_KEY's docs above) — a genuinely new
  // tab/app-restart finds nothing here and starts empty, which is exactly
  // the desired behavior. Prefer the cached message list (badges intact)
  // and only fall back to the network when there's no cache to hydrate
  // from, e.g. the pointer survived but the cache didn't.
  useEffect(() => {
    if (restoredRef.current) return;
    restoredRef.current = true;
    const stored = sessionStorage.getItem(ACTIVE_SESSION_KEY);
    if (!stored) return;
    onSessionId(stored);

    const cached = readCachedMessages(stored);
    if (cached) {
      setMessages(cached);
      onTurnComplete([]); // restoring history touches nothing new
      return;
    }

    api
      .getMessages(stored)
      .then((history) => {
        setMessages(
          history.map((m, i) => ({
            key: `restored-${i}-${m.created_at}`,
            role: m.role === "USER" ? "user" : "assistant",
            content: m.content,
            // handled_by isn't part of stored conversation history — only
            // known for messages sent/cached live in this browser tab.
          })),
        );
        onTurnComplete([]);
      })
      .catch((err) => {
        // Stored session no longer exists server-side (e.g. fresh DB) —
        // start clean rather than failing silently forever.
        console.warn("Could not restore session, starting fresh:", err);
        sessionStorage.removeItem(ACTIVE_SESSION_KEY);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep the cached message list in sync with the active session on every
  // change, so a same-tab reload always has an up-to-date, badge-carrying
  // snapshot to hydrate from.
  useEffect(() => {
    if (!sessionId) return;
    writeCachedMessages(sessionId, messages);
  }, [sessionId, messages]);

  useEffect(() => {
    listEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  // While a request is in flight and we already know the session id (i.e.
  // this isn't the very first message of a brand new session), poll the
  // same trace endpoint TracePanel/useTrace uses so the compact status
  // line below can show real intermediate progress — see useTrace.ts's
  // docstring for why polling (not streaming) is the real mechanism here.
  useEffect(() => {
    if (!sending || !sessionId) return;
    const activeSessionId = sessionId;
    const baseline = baselineEventCountRef.current;
    const interval = window.setInterval(() => {
      api
        .getTrace(activeSessionId)
        .then((events) => {
          const newEvents = events.slice(baseline);
          if (newEvents.length === 0) return;
          setLiveStatus(describeTraceEvent(newEvents[newEvents.length - 1]));
        })
        .catch(() => {
          // Best-effort polling — a transient failure just skips this tick.
        });
    }, 700);
    return () => window.clearInterval(interval);
  }, [sending, sessionId]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || sending) return;

    setMessages((prev) => [...prev, { key: `user-${Date.now()}`, role: "user", content: text }]);
    setInput("");
    setSending(true);
    setError(null);
    setLiveStatus("Invoking agent…");
    baselineEventCountRef.current = sessionId ? trace.events.length : 0;

    try {
      const res = await api.sendChat(text, sessionId ?? undefined);
      if (!sessionId) {
        sessionStorage.setItem(ACTIVE_SESSION_KEY, res.session_id);
        onSessionId(res.session_id);
        setPreviousSessions(appendPreviousSession(res.session_id, text));
      }

      let progressSummary: string | undefined;
      if (res.handled_by === "agent") {
        try {
          const finalEvents = await api.getTrace(res.session_id);
          progressSummary = summarizeToolCalls(finalEvents.slice(baselineEventCountRef.current));
        } catch {
          // Final trace refresh is a nice-to-have for the collapsed
          // summary — don't let it block showing the actual answer.
        }
      }

      setMessages((prev) => [
        ...prev,
        {
          key: `assistant-${Date.now()}`,
          role: "assistant",
          content: res.message,
          handledBy: res.handled_by,
          terminatedReason: res.terminated_reason,
          progressSummary,
        },
      ]);
      onTurnComplete(res.touched_entity_codes);
    } catch (err) {
      setError(String(err));
      onTurnComplete([]); // a failed request touched nothing
    } finally {
      setSending(false);
      setLiveStatus(null);
    }
  }

  function handleNewChat() {
    sessionStorage.removeItem(ACTIVE_SESSION_KEY);
    sessionStorage.removeItem(CACHED_MESSAGES_KEY);
    setMessages([]);
    setError(null);
    onSessionId(null);
    onTurnComplete([]);
  }

  async function handleLoadPreviousSession(id: string) {
    setError(null);
    try {
      const history = await api.getMessages(id);
      setMessages(
        history.map((m, i) => ({
          key: `restored-${i}-${m.created_at}`,
          role: m.role === "USER" ? "user" : "assistant",
          content: m.content,
          // Same API limitation as the mount-time fallback above: this
          // endpoint never had handled_by to give back, so older sessions
          // loaded this way won't show badges. Pre-existing, not fixed here.
        })),
      );
      sessionStorage.setItem(ACTIVE_SESSION_KEY, id);
      onSessionId(id);
      onTurnComplete([]);
    } catch (err) {
      setError(String(err));
    }
  }

  return (
    <div className="chat-panel">
      <div className="chat-panel-header">
        <h2>Chat</h2>
        <button type="button" className="chat-new-button" onClick={handleNewChat}>
          New chat
        </button>
      </div>
      {previousSessions.length > 0 && (
        <details className="chat-previous-sessions">
          <summary>Previous chats ({previousSessions.length})</summary>
          <ul>
            {previousSessions.map((s) => (
              <li key={s.session_id}>
                <button type="button" onClick={() => handleLoadPreviousSession(s.session_id)}>
                  <span className="chat-previous-preview">{s.last_message_preview}</span>
                  <span className="chat-previous-date">{new Date(s.started_at).toLocaleString()}</span>
                </button>
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className="chat-messages">
        {messages.length === 0 && (
          <p className="muted">
            Ask something like "list departments" (deterministic) or "what MRI scanners are free
            for the delayed appointments today?" (agent).
          </p>
        )}
        {messages.map((m) => (
          <div key={m.key} className={`chat-message chat-message-${m.role}`}>
            <div className="chat-message-meta">
              <span className="chat-role">{m.role === "user" ? "You" : "Assistant"}</span>
              <HandledByBadge handledBy={m.handledBy} />
            </div>
            {m.progressSummary && <div className="chat-progress-summary">{m.progressSummary}</div>}
            <div className="chat-message-content">
              {m.role === "assistant" ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
              ) : (
                m.content
              )}
            </div>
            {m.terminatedReason && (
              <div className="chat-terminated-reason">terminated: {m.terminatedReason}</div>
            )}
          </div>
        ))}
        {(sending || voice.active) && (
          <div className="chat-message chat-message-assistant chat-message-pending">
            <div className="chat-message-meta">
              <span className="chat-role">Assistant</span>
            </div>
            <div className="chat-progress-line">
              {sending ? (liveStatus ?? "Invoking agent…") : (voice.status ?? "Listening…")}
            </div>
          </div>
        )}
        <div ref={listEndRef} />
      </div>
      {error && <p className="error">{error}</p>}
      <form className="chat-input-row" onSubmit={handleSubmit}>
        <input
          type="text"
          value={input}
          placeholder="Type a message…"
          onChange={(e) => setInput(e.target.value)}
          disabled={sending || voice.active}
        />
        <button type="submit" disabled={sending || voice.active || !input.trim()}>
          {sending ? "Sending…" : "Send"}
        </button>
        <button
          type="button"
          className={`voice-mic-button${voice.active ? " voice-mic-button-active" : ""}`}
          onClick={voice.active ? voice.stop : handleStartVoice}
          disabled={sending}
          aria-pressed={voice.active}
          aria-label={voice.active ? "Stop voice input" : "Start voice input"}
          title={voice.active ? "Stop voice input" : "Start voice input"}
        >
          {voice.active ? "◼" : "\u{1F3A4}"}
        </button>
      </form>
    </div>
  );
}

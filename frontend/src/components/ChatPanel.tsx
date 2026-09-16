import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api/client";
import type { HandledBy } from "../api/types";

const SESSION_STORAGE_KEY = "shoa_session_id";

interface DisplayMessage {
  key: string;
  role: "user" | "assistant";
  content: string;
  handledBy?: HandledBy;
  terminatedReason?: string | null;
}

const HANDLED_BY_LABEL: Record<HandledBy, string> = {
  deterministic: "deterministic",
  agent: "agent",
  rejected: "rejected",
};

function HandledByBadge({ handledBy }: { handledBy?: HandledBy }) {
  if (!handledBy) return null;
  return <span className={`handled-by-badge handled-by-${handledBy}`}>{HANDLED_BY_LABEL[handledBy]}</span>;
}

interface ChatPanelProps {
  sessionId: string | null;
  onSessionId: (id: string) => void;
  onTurnComplete: () => void;
}

export function ChatPanel({ sessionId, onSessionId, onTurnComplete }: ChatPanelProps) {
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restoredRef = useRef(false);
  const listEndRef = useRef<HTMLDivElement | null>(null);

  // On mount: if a session_id is already stored from a previous page load,
  // restore the transcript and let the trace panel know there's a session
  // to fetch a trace for.
  useEffect(() => {
    if (restoredRef.current) return;
    restoredRef.current = true;
    const stored = localStorage.getItem(SESSION_STORAGE_KEY);
    if (!stored) return;
    onSessionId(stored);
    api
      .getMessages(stored)
      .then((history) => {
        setMessages(
          history.map((m, i) => ({
            key: `restored-${i}-${m.created_at}`,
            role: m.role === "USER" ? "user" : "assistant",
            content: m.content,
            // handled_by isn't part of stored conversation history — only
            // known for messages sent in this browser session.
          })),
        );
        onTurnComplete();
      })
      .catch((err) => {
        // Stored session no longer exists server-side (e.g. fresh DB) —
        // start clean rather than failing silently forever.
        console.warn("Could not restore session, starting fresh:", err);
        localStorage.removeItem(SESSION_STORAGE_KEY);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    listEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || sending) return;

    setMessages((prev) => [...prev, { key: `user-${Date.now()}`, role: "user", content: text }]);
    setInput("");
    setSending(true);
    setError(null);

    try {
      const res = await api.sendChat(text, sessionId ?? undefined);
      if (!sessionId) {
        localStorage.setItem(SESSION_STORAGE_KEY, res.session_id);
        onSessionId(res.session_id);
      }
      setMessages((prev) => [
        ...prev,
        {
          key: `assistant-${Date.now()}`,
          role: "assistant",
          content: res.message,
          handledBy: res.handled_by,
          terminatedReason: res.terminated_reason,
        },
      ]);
    } catch (err) {
      setError(String(err));
    } finally {
      setSending(false);
      onTurnComplete();
    }
  }

  return (
    <div className="chat-panel">
      <h2>Chat</h2>
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
            <div className="chat-message-content">{m.content}</div>
            {m.terminatedReason && (
              <div className="chat-terminated-reason">terminated: {m.terminatedReason}</div>
            )}
          </div>
        ))}
        <div ref={listEndRef} />
      </div>
      {error && <p className="error">{error}</p>}
      <form className="chat-input-row" onSubmit={handleSubmit}>
        <input
          type="text"
          value={input}
          placeholder="Type a message…"
          onChange={(e) => setInput(e.target.value)}
          disabled={sending}
        />
        <button type="submit" disabled={sending || !input.trim()}>
          {sending ? "Sending…" : "Send"}
        </button>
      </form>
    </div>
  );
}

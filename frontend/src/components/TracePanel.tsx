import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AgentEvent } from "../api/types";

interface TracePanelProps {
  sessionId: string | null;
  // Bumped by the parent after every completed chat turn so this panel
  // re-fetches — the backend's trace endpoint is polling-based by design
  // (see backend/app/api/routes/sessions.py's module docstring).
  refreshToken: number;
}

function formatEvent(e: AgentEvent): string {
  const parts = [`round ${e.round_num}`, e.event_type];
  if (e.tool_name) parts.push(e.tool_name);
  parts.push(e.status);
  if (e.latency_ms !== null) parts.push(`${e.latency_ms}ms`);
  if (e.error_category) parts.push(e.error_category);
  return parts.join(" · ");
}

export function TracePanel({ sessionId, refreshToken }: TracePanelProps) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [hasFetched, setHasFetched] = useState(false);

  useEffect(() => {
    if (!sessionId) {
      setEvents([]);
      setHasFetched(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getTrace(sessionId)
      .then((data) => {
        if (!cancelled) {
          setEvents(data);
          setHasFetched(true);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // refreshToken intentionally triggers a re-fetch even though it isn't
    // read in the body — it changes once per completed chat turn.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, refreshToken]);

  return (
    <div className="trace-panel">
      <h2>Trace</h2>
      <p className="trace-explainer muted">
        Action-level log of what the backend did to answer the last message: parser/agent
        routing, each tool call, and why anything was rejected. Deterministic-only requests
        never touch the agent, so they produce no rows here — that's expected.
      </p>
      {!sessionId && <p className="muted">Send a chat message to see its trace here.</p>}
      {sessionId && loading && <p className="muted">Loading trace…</p>}
      {error && <p className="error">{error}</p>}
      {sessionId && !loading && !error && hasFetched && events.length === 0 && (
        <p className="trace-empty-state">
          No agent activity in this session yet — the request(s) so far were handled
          deterministically (no LLM/tool calls involved), so there's nothing to trace.
        </p>
      )}
      {events.length > 0 && (
        <ol className="trace-list">
          {events.map((e, i) => (
            <li key={i} className={`trace-event trace-status-${e.status.toLowerCase()}`}>
              <div className="trace-event-line">{formatEvent(e)}</div>
              {e.arguments_json && (
                <details className="trace-event-args">
                  <summary>arguments</summary>
                  <pre>{JSON.stringify(e.arguments_json, null, 2)}</pre>
                </details>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

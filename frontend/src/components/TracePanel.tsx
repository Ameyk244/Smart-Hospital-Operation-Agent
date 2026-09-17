import type { AgentEvent } from "../api/types";
import type { TraceState } from "../hooks/useTrace";

interface TracePanelProps {
  sessionId: string | null;
  trace: TraceState;
}

function formatEvent(e: AgentEvent): string {
  const parts = [`round ${e.round_num}`, e.event_type];
  if (e.tool_name) parts.push(e.tool_name);
  parts.push(e.status);
  if (e.latency_ms !== null) parts.push(`${e.latency_ms}ms`);
  if (e.error_category) parts.push(e.error_category);
  return parts.join(" · ");
}

// Purely a renderer now — the fetch/poll lives in ../hooks/useTrace, shared
// with ChatPanel's compact in-chat progress indicator so there's exactly
// one place this project talks to GET /api/sessions/{id}/trace.
export function TracePanel({ sessionId, trace }: TracePanelProps) {
  const { events, loading, error, hasFetched } = trace;

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

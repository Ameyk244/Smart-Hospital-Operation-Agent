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

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

// Second line under a "jev_invoked" row: what the typed-decision model
// chose, how confident it was, and the threshold that confidence was
// compared against. Read defensively — arguments_json is an untyped bag
// from the backend, and a partially-populated one shouldn't blank the row.
function formatJevDetail(e: AgentEvent): string {
  const args = e.arguments_json ?? {};
  const confidence = asNumber(args.confidence);
  const threshold = asNumber(args.threshold);
  const inputTokens = asNumber(args.input_tokens);
  const outputTokens = asNumber(args.output_tokens);
  const model = asString(args.model);

  // Only a SUCCESS row actually routed through CommandRunner. On a REJECTED
  // row Jev did answer — and may well have named a real command — but below
  // the confidence threshold, so it was *not* acted on and the turn fell
  // through to the agent. Saying "matched list_scanners" there would
  // misdescribe the one thing this panel exists to report accurately. "none"
  // is Jev's explicit no-match option, not a command, so it never reads as a
  // name either.
  const answered = asString(args.choice);
  const named = answered && answered !== "none" ? answered : null;
  const matched = e.status === "SUCCESS" ? (named ?? e.tool_name) : null;

  const parts = [
    matched
      ? `matched ${matched}`
      : named
        ? `no match — closest ${named}`
        : "no command matched",
  ];
  if (confidence !== null) parts.push(`confidence ${formatPercent(confidence)}`);
  if (threshold !== null) parts.push(`threshold ${formatPercent(threshold)}`);
  if (inputTokens !== null || outputTokens !== null) {
    parts.push(`tokens ${inputTokens ?? "?"} in / ${outputTokens ?? "?"} out`);
  }
  if (model) parts.push(model);
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
        never touch the agent, so they produce no rows here — that's expected. A
        "jev_invoked" row means the typed-decision fast path was consulted before the agent;
        it appears whether or not that path ended up handling the message.
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
              {e.event_type === "jev_invoked" && (
                <div className="trace-event-detail">{formatJevDetail(e)}</div>
              )}
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

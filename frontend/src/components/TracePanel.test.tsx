import { describe, expect, it, beforeEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { TracePanel } from "./TracePanel";
import type { AgentEvent } from "../api/types";
import type { TraceState } from "../hooks/useTrace";

// TracePanel is a pure renderer (fetching lives in useTrace), so these
// tests hand it a ready-made TraceState instead of mocking fetch at all.
function traceWith(events: AgentEvent[]): TraceState {
  return { events, loading: false, error: null, hasFetched: true };
}

function jevEvent(overrides: Partial<AgentEvent> = {}): AgentEvent {
  return {
    round_num: 0,
    event_type: "jev_invoked",
    tool_name: "list_departments",
    arguments_json: {
      choice: "list_departments",
      confidence: 0.92,
      probabilities: { list_departments: 0.92, list_scanners: 0.05 },
      threshold: 0.7,
      input_tokens: 18,
      output_tokens: 3,
      model: "jev-1",
    },
    status: "SUCCESS",
    latency_ms: 42,
    error_category: null,
    created_at: "t1",
    ...overrides,
  };
}

beforeEach(() => {
  cleanup();
});

describe("TracePanel jev_invoked rendering", () => {
  it("renders the matched command, confidence, threshold, latency and token usage", () => {
    render(<TracePanel sessionId="sess-1" trace={traceWith([jevEvent()])} />);

    // Standard event line, same shape as every other event type. Matched
    // via "round 0 ·" so this can't accidentally hit the panel's
    // explainer paragraph, which also mentions jev_invoked.
    const line = screen.getByText(/round 0 · jev_invoked/);
    expect(line).toHaveTextContent("list_departments");
    expect(line).toHaveTextContent("42ms");

    const detail = screen.getByText(/matched list_departments/);
    expect(detail).toHaveTextContent("confidence 92.0%");
    expect(detail).toHaveTextContent("threshold 70.0%");
    expect(detail).toHaveTextContent("tokens 18 in / 3 out");
  });

  it("says nothing matched and surfaces the error category on a FAILURE event", () => {
    render(
      <TracePanel
        sessionId="sess-1"
        trace={traceWith([
          jevEvent({
            tool_name: null,
            status: "FAILURE",
            error_category: "jev_timeout",
            arguments_json: {
              choice: null,
              confidence: null,
              probabilities: null,
              threshold: 0.7,
              input_tokens: null,
              output_tokens: null,
              model: null,
            },
          }),
        ])}
      />,
    );

    expect(screen.getByText(/round 0 · jev_invoked/)).toHaveTextContent("jev_timeout");
    expect(screen.getByText(/no command matched/)).toBeInTheDocument();
  });

  it("does not claim a match when Jev named a command but fell below the threshold", () => {
    // The misleading case: Jev answered "list_scanners" at 0.55 against a
    // 0.7 threshold, so tool_name is null and the turn went to the agent.
    // Rendering this as "matched list_scanners" would describe an execution
    // that never happened.
    render(
      <TracePanel
        sessionId="sess-1"
        trace={traceWith([
          jevEvent({
            tool_name: null,
            status: "REJECTED",
            arguments_json: {
              choice: "list_scanners",
              confidence: 0.55,
              probabilities: { list_scanners: 0.55, none: 0.3 },
              threshold: 0.7,
              input_tokens: 18,
              output_tokens: 3,
              model: "jev-1",
            },
          }),
        ])}
      />,
    );

    const detail = screen.getByText(/no match — closest list_scanners/);
    expect(detail).toHaveTextContent("confidence 55.0%");
    expect(screen.queryByText(/matched list_scanners/)).not.toBeInTheDocument();
  });

  it("reads Jev's explicit 'none' answer as no match rather than a command named none", () => {
    render(
      <TracePanel
        sessionId="sess-1"
        trace={traceWith([
          jevEvent({
            tool_name: null,
            status: "REJECTED",
            arguments_json: {
              choice: "none",
              confidence: 0.95,
              probabilities: { none: 0.95 },
              threshold: 0.7,
              input_tokens: 18,
              output_tokens: 3,
              model: "jev-1",
            },
          }),
        ])}
      />,
    );

    expect(screen.getByText(/no command matched/)).toBeInTheDocument();
  });
});

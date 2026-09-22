import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { CostComparisonPage } from "./CostComparisonPage";
import type { CostComparison } from "../api/types";

// Same shape as ChatPanel.test.tsx's createFetchMock: a tiny router over
// the /api/* paths this component actually touches, so nothing ever hits
// the network and an unexpected call fails loudly.
function jsonResponse(data: unknown): Response {
  return { ok: true, json: () => Promise.resolve(data) } as Response;
}

function createFetchMock(payload: CostComparison) {
  return vi.fn((url: string, init?: RequestInit) => {
    const u = String(url);
    if (u.includes("/api/cost-comparison")) {
      return Promise.resolve(jsonResponse(payload));
    }
    return Promise.reject(new Error(`Unhandled fetch in test: ${init?.method ?? "GET"} ${u}`));
  });
}

const POPULATED: CostComparison = {
  jev_calls_total: 12,
  jev_fast_path_hits: 7,
  jev_fallthroughs: 5,
  jev_input_tokens: 3400,
  jev_cost_usd: 0.000143,
  avoided_agent_turns: 7,
  avoided_sonnet_cost_usd: 0.035,
  net_savings_usd: 0.0349,
  assumptions: {
    sonnet_input_per_mtok_usd: 2.0,
    measured_system_prompt_tokens: 440,
    measured_tool_schema_tokens: 1395,
  },
};

beforeEach(() => {
  cleanup();
});

describe("CostComparisonPage", () => {
  it("renders the tally figures from /api/cost-comparison", async () => {
    vi.stubGlobal("fetch", createFetchMock(POPULATED));

    render(<CostComparisonPage onBack={vi.fn()} />);

    expect(await screen.findByText("$0.000143")).toBeInTheDocument();
    expect(screen.getByText("Jev calls made").closest("tr")).toHaveTextContent("12");
    expect(screen.getByText(/Fast-path hits/).closest("tr")).toHaveTextContent("7");
    expect(screen.getByText(/Fall-throughs/).closest("tr")).toHaveTextContent("5");
    expect(screen.getByText("Net savings").closest("tr")).toHaveTextContent("$0.034900");
  });

  it("shows its assumptions and says plainly that the without-Jev figure is an estimate", async () => {
    vi.stubGlobal("fetch", createFetchMock(POPULATED));

    render(<CostComparisonPage onBack={vi.fn()} />);

    expect(await screen.findByText(/is an estimate, not a measurement/)).toBeInTheDocument();
    // Assumption keys are rendered generically, not hardcoded per key.
    // Loose on the thousands separator: the value goes through
    // toLocaleString, so the grouping character is environment-dependent.
    expect(screen.getByText("measured tool schema tokens").closest("tr")).toHaveTextContent(
      /1[,.\s ]?395/,
    );
    expect(screen.getByText("sonnet input per mtok usd")).toBeInTheDocument();
  });

  it("explains the empty case instead of showing a savings story when no Jev calls have happened", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        jev_calls_total: 0,
        jev_fast_path_hits: 0,
        jev_fallthroughs: 0,
        jev_input_tokens: 0,
        jev_cost_usd: 0,
        avoided_agent_turns: 0,
        avoided_sonnet_cost_usd: 0,
        net_savings_usd: 0,
        assumptions: {},
      }),
    );

    render(<CostComparisonPage onBack={vi.fn()} />);

    expect(await screen.findByText(/No Jev calls have been made yet/)).toBeInTheDocument();
    expect(screen.getByText("Jev calls made").closest("tr")).toHaveTextContent("0");
  });

  it("surfaces a fetch failure rather than rendering a blank page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 500, statusText: "Server Error", json: () => Promise.reject(new Error("no json")) } as unknown as Response)),
    );

    render(<CostComparisonPage onBack={vi.fn()} />);

    expect(await screen.findByText(/Server Error/)).toBeInTheDocument();
  });
});

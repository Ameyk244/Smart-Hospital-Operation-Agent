import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AlertsPage } from "./AlertsPage";
import { AlertBell } from "./AlertBell";
import type { Alert, ScheduledTask } from "../api/types";

const ALERTS: Alert[] = [
  {
    id: 5,
    task_code: "TSK-1",
    severity: "critical",
    summary: "ER-1 has waited 45 minutes",
    entity_codes: ["ER-1"],
    created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    acknowledged_at: null,
  },
];

const TASKS: ScheduledTask[] = [
  {
    code: "TSK-1",
    condition: "QUEUE_WAIT_OVER",
    threshold: 30,
    scope_code: null,
    period_seconds: 300,
    active: true,
    last_run_at: null,
  },
  {
    code: "TSK-2",
    condition: "FREE_BEDS_UNDER",
    threshold: 2,
    scope_code: "WRD-1",
    period_seconds: 300,
    active: true,
    last_run_at: new Date(Date.now() - 2 * 60_000).toISOString(),
  },
];

function mockFetchReturning(data: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve(data) } as Response)),
  );
}

describe("AlertsPage", () => {
  it("renders open alerts and active watches", async () => {
    mockFetchReturning(TASKS);
    render(<AlertsPage alerts={ALERTS} />);

    expect(screen.getByText("ER-1 has waited 45 minutes")).toBeInTheDocument();
    expect(screen.getByText("critical")).toHaveClass("pg-badge-critical");
    expect(screen.getByText(/ER-1 · 5 min ago/)).toBeInTheDocument();
    expect(await screen.findByText("Emergency queue wait over 30 minutes")).toBeInTheDocument();
    expect(screen.getByText("Free beds under 2")).toBeInTheDocument();
    expect(screen.getByText("WRD-1")).toBeInTheDocument();
    expect(screen.getByText("Whole hospital")).toBeInTheDocument();
    expect(screen.getByText("never")).toBeInTheDocument();
    expect(screen.getByText("2 min ago")).toBeInTheDocument();
    expect(screen.getAllByText("every 5 min")).toHaveLength(2);
  });

  it("shows empty states that point to chat", async () => {
    mockFetchReturning([]);
    render(<AlertsPage alerts={[]} />);

    expect(await screen.findByText(/No active watches/)).toBeInTheDocument();
    expect(screen.getByText(/No open alerts/)).toBeInTheDocument();
    expect(screen.getAllByText(/watch queue wait over 30 minutes/)).toHaveLength(2);
  });
});

describe("AlertBell", () => {
  it("shows the open count and opens the page on click", () => {
    const onOpen = vi.fn();
    render(<AlertBell schedulerOn count={3} onOpen={onOpen} />);

    expect(screen.getByText("3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button"));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("hides the count at zero", () => {
    render(<AlertBell schedulerOn count={0} onOpen={() => {}} />);

    expect(screen.getByRole("button")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("is hidden when the scheduler is off", () => {
    render(<AlertBell schedulerOn={false} count={3} onOpen={() => {}} />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

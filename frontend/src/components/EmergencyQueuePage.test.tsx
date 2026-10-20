import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { EmergencyQueuePage } from "./EmergencyQueuePage";
import type { QueueEntry } from "../api/types";

const ENTRIES: QueueEntry[] = [
  {
    code: "ER-1",
    patient: { code: "PAT-1", name: "Ada Lovelace" },
    urgency: "STANDARD",
    status: "WAITING",
    arrived_at: "2026-01-01T10:00:00Z",
    triaged_at: null,
    bed: null,
    discharged_at: null,
    wait_minutes: 90,
  },
  {
    code: "ER-2",
    patient: { code: "PAT-2", name: "Alan Turing" },
    urgency: "CRITICAL",
    status: "ADMITTED",
    arrived_at: "2026-01-01T10:30:00Z",
    triaged_at: "2026-01-01T10:35:00Z",
    bed: { code: "BED-3", ward: { code: "WRD-1", name: "Ward A" } },
    discharged_at: null,
    wait_minutes: 0,
  },
  {
    code: "ER-3",
    patient: { code: "PAT-3", name: "Grace Hopper" },
    urgency: "URGENT",
    status: "WAITING",
    arrived_at: "2026-01-01T10:20:00Z",
    triaged_at: null,
    bed: null,
    discharged_at: null,
    wait_minutes: 20,
  },
];

function mockFetchReturning(data: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve(data) } as Response)),
  );
}

describe("EmergencyQueuePage", () => {
  it("lists WAITING by urgency first, then admitted, and shows wait only for WAITING", async () => {
    mockFetchReturning(ENTRIES);
    render(<EmergencyQueuePage touchedEntityCodes={[]} />);

    await screen.findByText("ER-1");
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("ER-3");
    expect(rows[1]).toHaveTextContent("ER-1");
    expect(rows[2]).toHaveTextContent("ER-2");
    expect(screen.getByText("1h 30m")).toBeInTheDocument();
    expect(screen.getByText("20 min")).toBeInTheDocument();
    expect(rows[2].querySelector(".pg-wait")).toHaveTextContent("—");
    expect(screen.getByText("BED-3 (Ward A)")).toBeInTheDocument();
  });

  it("marks only the touched row", async () => {
    mockFetchReturning(ENTRIES);
    render(<EmergencyQueuePage touchedEntityCodes={["ER-1"]} />);

    expect((await screen.findByText("ER-1")).closest("tr")).toHaveClass("row-touched");
    expect(screen.getByText("ER-2").closest("tr")).not.toHaveClass("row-touched");
  });

  it("shows an empty state", async () => {
    mockFetchReturning([]);
    render(<EmergencyQueuePage touchedEntityCodes={[]} />);
    expect(await screen.findByText("No queue entries match these filters.")).toBeInTheDocument();
  });
});

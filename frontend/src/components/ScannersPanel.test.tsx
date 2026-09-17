import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ScannersPanel } from "./ScannersPanel";
import type { Scanner } from "../api/types";

const SCANNERS: Scanner[] = [
  { code: "SCN-01", name: "MRI Suite 1", type: "MRI", status: "AVAILABLE" },
  { code: "SCN-02", name: "CT Suite 1", type: "CT", status: "IN_USE" },
];

function mockFetchReturning(data: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(data),
      } as Response),
    ),
  );
}

// Covers the Operations-panel "row touched by this chat turn" highlight:
// ScannersPanel already derives className="row-touched" from the
// touchedEntityCodes prop it's handed (see App.tsx -> OperationsView),
// these tests just pin that behavior down per row.
describe("ScannersPanel row highlighting", () => {
  beforeEach(() => {
    mockFetchReturning(SCANNERS);
  });

  it("marks only the row whose code is in touchedEntityCodes", async () => {
    render(<ScannersPanel touchedEntityCodes={["SCN-02"]} />);

    const untouchedCell = await screen.findByText("SCN-01");
    const touchedCell = await screen.findByText("SCN-02");

    expect(untouchedCell.closest("tr")).not.toHaveClass("row-touched");
    expect(touchedCell.closest("tr")).toHaveClass("row-touched");
  });

  it("marks no row when touchedEntityCodes is empty", async () => {
    render(<ScannersPanel touchedEntityCodes={[]} />);

    const cell1 = await screen.findByText("SCN-01");
    const cell2 = await screen.findByText("SCN-02");

    expect(cell1.closest("tr")).not.toHaveClass("row-touched");
    expect(cell2.closest("tr")).not.toHaveClass("row-touched");
  });

  it("marks no row when touchedEntityCodes doesn't contain this row's code", async () => {
    render(<ScannersPanel touchedEntityCodes={["SCN-99"]} />);

    const cell1 = await screen.findByText("SCN-01");
    const cell2 = await screen.findByText("SCN-02");

    expect(cell1.closest("tr")).not.toHaveClass("row-touched");
    expect(cell2.closest("tr")).not.toHaveClass("row-touched");
  });
});

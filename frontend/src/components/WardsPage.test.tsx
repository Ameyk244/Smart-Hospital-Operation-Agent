import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { WardsPage } from "./WardsPage";
import type { Bed, Ward } from "../api/types";

const WARDS: Ward[] = [
  { code: "WRD-1", name: "Ward A", department: { code: "DEP-1", name: "Cardiology" }, bed_count: 2, occupied_count: 1 },
];
const BEDS: Bed[] = [
  { code: "BED-1", ward: { code: "WRD-1", name: "Ward A" }, status: "AVAILABLE", patient: null, occupied_since: null },
  {
    code: "BED-2",
    ward: { code: "WRD-1", name: "Ward A" },
    status: "OCCUPIED",
    patient: { code: "PAT-1", name: "Ada Lovelace", mrn: "MRN-1" },
    occupied_since: "2026-01-01T10:00:00Z",
  },
];

function mockFetch(wards: unknown, beds: unknown, ok = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      Promise.resolve({
        ok,
        statusText: "Server Error",
        json: () => Promise.resolve(url.includes("/wards") ? wards : beds),
      } as Response),
    ),
  );
}

describe("WardsPage", () => {
  it("shows occupancy and highlights only the touched bed", async () => {
    mockFetch(WARDS, BEDS);
    render(<WardsPage touchedEntityCodes={["BED-2"]} />);

    expect(await screen.findByText("1/2 occupied")).toBeInTheDocument();
    expect((await screen.findByText("BED-2")).closest("tr")).toHaveClass("row-touched");
    expect(screen.getByText("BED-1").closest("tr")).not.toHaveClass("row-touched");
  });

  it("shows an empty state when there are no wards", async () => {
    mockFetch([], []);
    render(<WardsPage touchedEntityCodes={[]} />);
    expect(await screen.findByText("No wards found.")).toBeInTheDocument();
  });

  it("shows an error when the request fails", async () => {
    mockFetch({ detail: "boom" }, [], false);
    render(<WardsPage touchedEntityCodes={[]} />);
    expect(await screen.findByText(/boom/)).toBeInTheDocument();
  });
});

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { JourneyPage } from "./JourneyPage";
import type { Journey, Patient } from "../api/types";

const PATIENTS: Patient[] = [
  { code: "PAT-1", mrn: "MRN-1", name: "Ada Lovelace", date_of_birth: "1990-01-01" },
];
const JOURNEY: Journey = {
  patient: { code: "PAT-1", name: "Ada Lovelace" },
  stages: [
    { stage: "ARRIVED", at: "2026-01-01T10:00:00Z", detail: null },
    { stage: "TRIAGED", at: "2026-01-01T10:10:00Z", detail: null },
    { stage: "BED_ASSIGNED", at: null, detail: null },
    { stage: "SCAN", at: null, detail: null },
    { stage: "DISCHARGED", at: null, detail: null },
  ],
  current_stage: "TRIAGED",
  appointments: [],
};

function mockFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(url.includes("/journey") ? JOURNEY : PATIENTS),
      } as Response),
    ),
  );
}

describe("JourneyPage", () => {
  it("prompts for a search before anything is chosen", () => {
    mockFetch();
    render(<JourneyPage touchedEntityCodes={[]} />);
    expect(screen.getByText("Search for a patient to see their journey.")).toBeInTheDocument();
  });

  it("shows the stepper with the current stage emphasised", async () => {
    mockFetch();
    render(<JourneyPage touchedEntityCodes={[]} />);

    fireEvent.change(screen.getByLabelText("Patient name"), { target: { value: "Ada" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));

    const current = (await screen.findByText("TRIAGED")).closest("li");
    expect(current).toHaveClass("pg-step-current");
    expect(screen.getByText("ARRIVED").closest("li")).not.toHaveClass("pg-step-current");
    expect(screen.getByText("No appointments for this patient.")).toBeInTheDocument();
  });
});

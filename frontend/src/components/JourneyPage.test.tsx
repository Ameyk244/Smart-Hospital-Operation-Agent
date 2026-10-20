import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { JourneyPage } from "./JourneyPage";
import type { Journey, Patient } from "../api/types";

const PATIENTS: Patient[] = [
  { code: "PAT-1", mrn: "MRN-1", name: "Ada Lovelace", date_of_birth: "1990-01-01" },
];
const JOURNEY: Journey = {
  patient: { code: "PAT-1", name: "Ada Lovelace" },
  stages: [
    { stage: "ARRIVED", reached: true, at: "2026-01-01T10:00:00Z", detail: null },
    { stage: "TRIAGED", reached: true, at: "2026-01-01T10:10:00Z", detail: null },
    { stage: "BED_ASSIGNED", reached: false, at: null, detail: null },
    { stage: "SCAN", reached: false, at: null, detail: null },
    { stage: "DISCHARGED", reached: false, at: null, detail: null },
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
    expect(screen.getByText("ARRIVED").closest("li")).toHaveClass("pg-step-done");
    expect(screen.getByText("SCAN").closest("li")).not.toHaveClass("pg-step-done");
    expect(screen.getAllByText("Not reached")).toHaveLength(3);
  });

  it("shows a reached stage with no timestamp as done", async () => {
    const journey: Journey = {
      ...JOURNEY,
      stages: [{ stage: "ARRIVED", reached: true, at: null, detail: null }],
      current_stage: "ARRIVED",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve({
          ok: true,
          json: () => Promise.resolve(url.includes("/journey") ? journey : PATIENTS),
        } as Response),
      ),
    );
    render(<JourneyPage touchedEntityCodes={[]} />);

    fireEvent.change(screen.getByLabelText("Patient name"), { target: { value: "Ada" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));

    const step = (await screen.findByText("ARRIVED")).closest("li");
    expect(step).toHaveClass("pg-step-done");
    expect(step).toHaveTextContent("—");
    expect(screen.queryByText("Not reached")).not.toBeInTheDocument();
  });

  it("re-fetches the selected patient's journey when touchedEntityCodes changes", async () => {
    mockFetch();
    const fetchMock = vi.mocked(fetch);
    const journeyCalls = () => fetchMock.mock.calls.filter(([u]) => String(u).includes("/journey")).length;
    const { rerender } = render(<JourneyPage touchedEntityCodes={[]} />);

    fireEvent.change(screen.getByLabelText("Patient name"), { target: { value: "Ada" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await screen.findByText("TRIAGED");
    expect(journeyCalls()).toBe(1);

    rerender(<JourneyPage touchedEntityCodes={["BED-1"]} />);

    await waitFor(() => expect(journeyCalls()).toBe(2));
    expect(screen.getByText("TRIAGED")).toBeInTheDocument();
  });

  it("explains an empty journey and still shows the appointments table", async () => {
    const journey: Journey = { ...JOURNEY, stages: [], current_stage: "NONE" };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve({
          ok: true,
          json: () => Promise.resolve(url.includes("/journey") ? journey : PATIENTS),
        } as Response),
      ),
    );
    render(<JourneyPage touchedEntityCodes={[]} />);

    fireEvent.change(screen.getByLabelText("Patient name"), { target: { value: "Ada" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));

    expect(await screen.findByText("No emergency visit for this patient.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Journey stages" })).not.toBeInTheDocument();
    expect(screen.getByText("No appointments for this patient.")).toBeInTheDocument();
  });

  it("applies only the latest selection when responses arrive out of order", async () => {
    const two: Patient[] = [
      PATIENTS[0],
      { code: "PAT-2", mrn: "MRN-2", name: "Alan Turing", date_of_birth: "1912-06-23" },
    ];
    const resolvers: Record<string, (j: Journey) => void> = {};
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (!url.includes("/journey")) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(two) } as Response);
        }
        const code = url.includes("PAT-2") ? "PAT-2" : "PAT-1";
        return new Promise((resolve) => {
          resolvers[code] = (j) =>
            resolve({ ok: true, json: () => Promise.resolve(j) } as Response);
        });
      }),
    );
    render(<JourneyPage touchedEntityCodes={[]} />);

    fireEvent.change(screen.getByLabelText("Patient name"), { target: { value: "a" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    fireEvent.click(screen.getByRole("button", { name: /Alan Turing/ }));

    await waitFor(() => expect(resolvers["PAT-2"]).toBeDefined());
    resolvers["PAT-2"]({ ...JOURNEY, patient: { code: "PAT-2", name: "Alan Turing" } });
    expect(await screen.findByRole("heading", { name: "Alan Turing (PAT-2)" })).toBeInTheDocument();

    resolvers["PAT-1"](JOURNEY);
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByRole("heading", { name: "Alan Turing (PAT-2)" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Ada Lovelace (PAT-1)" })).not.toBeInTheDocument();
  });
});

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { OperationsView } from "./components/OperationsView";

// Proves the Vitest + Testing Library + jsdom setup actually works before
// anyone builds real tests on top of it — not a test of app behavior.
describe("frontend test setup", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          json: () => Promise.resolve([]),
        } as Response),
      ),
    );
  });

  it("renders a real component without crashing", () => {
    render(<OperationsView touchedEntityCodes={[]} />);
    expect(screen.getByText("Hospital operations")).toBeInTheDocument();
  });
});

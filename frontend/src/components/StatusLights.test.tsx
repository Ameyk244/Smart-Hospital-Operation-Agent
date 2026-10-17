import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, render, renderHook, screen, cleanup } from "@testing-library/react";
import { StatusLights } from "./StatusLights";
import { useStatus } from "../hooks/useStatus";
import type { StatusState } from "../hooks/useStatus";
import { api } from "../api/client";
import type { StatusResponse } from "../api/types";

// StatusLights is a pure renderer (polling lives in useStatus), so the
// component tests hand it a ready-made StatusState. The hook tests spy on
// api.getStatus so nothing ever hits the network.
const STATUS: StatusResponse = {
  agent: { runtime: "in_process", status: "on" },
  harness: { status: "off" },
  jev: { status: "on" },
};

function stateWith(status: StatusResponse, unreachable = false): StatusState {
  return { status, unreachable };
}

beforeEach(() => {
  cleanup();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("StatusLights", () => {
  it("renders on, off and down with a class and the state word, not colour alone", () => {
    render(
      <StatusLights
        state={stateWith({
          agent: { runtime: "in_process", status: "down" },
          harness: { status: "off" },
          jev: { status: "on" },
        })}
      />,
    );

    expect(screen.getByRole("list", { name: "System status" })).toBeInTheDocument();
    expect(screen.getByText("Agent").closest("li")).toHaveClass("status-light-down");
    expect(screen.getByText("Agent").closest("li")).toHaveTextContent("Agent: down");
    expect(screen.getByText("Harness").closest("li")).toHaveClass("status-light-off");
    expect(screen.getByText("Harness").closest("li")).toHaveTextContent("Harness: off");
    expect(screen.getByText("Jev").closest("li")).toHaveClass("status-light-on");
    expect(screen.getByText("Jev").closest("li")).toHaveTextContent("Jev: on");
  });

  it("says which runtime answers in the Agent tooltip", () => {
    const { rerender } = render(<StatusLights state={stateWith(STATUS)} />);
    expect(screen.getByTitle(/Agent: on/)).toHaveAttribute("title", expect.stringContaining("in-process"));

    rerender(
      <StatusLights state={stateWith({ ...STATUS, agent: { runtime: "harness", status: "on" } })} />,
    );
    expect(screen.getByTitle(/Agent: on/)).toHaveAttribute("title", expect.stringContaining("harness"));
  });

  it("shows every light as unknown when the backend is unreachable", () => {
    render(<StatusLights state={stateWith(STATUS, true)} />);

    for (const label of ["Agent", "Harness", "Jev"]) {
      const light = screen.getByText(label).closest("li");
      expect(light).toHaveClass("status-light-unknown");
      expect(light).toHaveTextContent(`${label}: unknown`);
    }
  });

  it("shows unknown before the first status arrives", () => {
    render(<StatusLights state={{ status: null, unreachable: false }} />);

    expect(screen.getByText("Jev").closest("li")).toHaveClass("status-light-unknown");
  });
});

const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));

describe("useStatus", () => {
  it("fetches on mount, polls every 15s and stops polling on unmount", async () => {
    vi.useFakeTimers();
    const getStatus = vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);

    const { result, unmount } = renderHook(() => useStatus());
    await advance(0);
    expect(getStatus).toHaveBeenCalledTimes(1);
    expect(result.current).toEqual({ status: STATUS, unreachable: false });

    await advance(15_000);
    expect(getStatus).toHaveBeenCalledTimes(2);

    unmount();
    await advance(45_000);
    expect(getStatus).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps the last known value but flags unreachable when a fetch fails", async () => {
    vi.useFakeTimers();
    const getStatus = vi.spyOn(api, "getStatus").mockResolvedValueOnce(STATUS);
    getStatus.mockRejectedValue(new Error("offline"));

    const { result } = renderHook(() => useStatus());
    await advance(0);
    expect(result.current.unreachable).toBe(false);

    await advance(15_000);
    expect(result.current).toEqual({ status: STATUS, unreachable: true });
  });
});

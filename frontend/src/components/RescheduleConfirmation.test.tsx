import { useState } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { ChatPanel } from "./ChatPanel";
import type { TraceState } from "../hooks/useTrace";
import type { PendingReschedule } from "../api/types";

const emptyTrace: TraceState = { events: [], loading: false, error: null, hasFetched: false };

const PROPOSAL: PendingReschedule = {
  proposal_id: 7,
  appointment_code: "APT-2001",
  scanner_code: "SCN-2",
  new_start: "2026-01-02T09:00:00Z",
};

function jsonResponse(data: unknown): Response {
  return { ok: true, json: () => Promise.resolve(data) } as Response;
}

function errorResponse(status: number, detail: string): Response {
  return { ok: false, status, statusText: "Error", json: () => Promise.resolve({ detail }) } as Response;
}

// Answers the chat, pending-reschedules and confirm/cancel endpoints; every
// other URL (trace, messages) is an unhandled fetch so a test can't depend on
// a call it didn't mean to make.
function createFetchMock(opts: {
  chatPending?: PendingReschedule[];
  listPending?: PendingReschedule[];
  resolve?: (action: "confirm" | "cancel") => Response;
}) {
  return vi.fn((url: string, init?: RequestInit) => {
    const u = String(url);
    if (u.includes("/api/chat") && init?.method === "POST") {
      return Promise.resolve(
        jsonResponse({
          session_id: "sess-1",
          handled_by: "agent",
          message: "I can move it. Please confirm.",
          data: null,
          terminated_reason: null,
          touched_entity_codes: [],
          pending_confirmations: opts.chatPending,
        }),
      );
    }
    if (u.endsWith("/api/sessions/sess-1/trace")) return Promise.resolve(jsonResponse([]));
    if (u.endsWith("/api/sessions/sess-1/pending-reschedules")) {
      return Promise.resolve(jsonResponse(opts.listPending ?? []));
    }
    const action = u.match(/\/api\/sessions\/sess-1\/reschedule\/7\/(confirm|cancel)$/);
    if (action && init?.method === "POST") {
      return Promise.resolve(opts.resolve?.(action[1] as "confirm" | "cancel"));
    }
    return Promise.reject(new Error(`Unhandled fetch in test: ${init?.method ?? "GET"} ${u}`));
  });
}

function Harness({ onTurnComplete }: { onTurnComplete: (codes: string[]) => void }) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  return (
    <ChatPanel sessionId={sessionId} onSessionId={setSessionId} onTurnComplete={onTurnComplete} trace={emptyTrace} />
  );
}

async function sendAndWaitForCard(onTurnComplete = vi.fn()) {
  render(<Harness onTurnComplete={onTurnComplete} />);
  fireEvent.change(screen.getByPlaceholderText("Type a message…"), { target: { value: "move APT-2001" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByText("I can move it. Please confirm.");
  return onTurnComplete;
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  cleanup();
});

describe("RescheduleConfirmation in the chat", () => {
  it("renders a card with Confirm and Cancel for each pending proposal", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chatPending: [PROPOSAL, { ...PROPOSAL, proposal_id: 8, appointment_code: "APT-2002", new_start: null }],
      }),
    );
    await sendAndWaitForCard();

    expect(screen.getByRole("region", { name: "Pending appointment moves" })).toBeInTheDocument();
    expect(screen.getByText(/Move APT-2001 to SCN-2 \(new time /)).toBeInTheDocument();
    expect(screen.getByText("Move APT-2002 to SCN-2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm moving APT-2001 to SCN-2" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Cancel moving APT-2002 to SCN-2" })).toBeEnabled();
  });

  it("renders nothing extra when the response has no pending_confirmations", async () => {
    vi.stubGlobal("fetch", createFetchMock({}));
    await sendAndWaitForCard();

    expect(screen.queryByRole("region", { name: "Pending appointment moves" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Confirm moving/ })).not.toBeInTheDocument();
  });

  it("Confirm posts the right ids, shows the message, removes the card and reports touched codes", async () => {
    const fetchMock = createFetchMock({
      chatPending: [PROPOSAL],
      resolve: () =>
        jsonResponse({ status: "confirmed", message: "APT-2001 moved to SCN-2.", touched_entity_codes: ["APT-2001", "SCN-2"] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const onTurnComplete = await sendAndWaitForCard();

    fireEvent.click(screen.getByRole("button", { name: "Confirm moving APT-2001 to SCN-2" }));

    expect(await screen.findByText("APT-2001 moved to SCN-2.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Confirm moving/ })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/sessions/sess-1/reschedule/7/confirm"),
      expect.objectContaining({ method: "POST" }),
    );
    expect(onTurnComplete).toHaveBeenLastCalledWith(["APT-2001", "SCN-2"]);
  });

  it("Cancel posts the right ids, shows the message and removes the card", async () => {
    const fetchMock = createFetchMock({
      chatPending: [PROPOSAL],
      resolve: () => jsonResponse({ status: "cancelled", message: "Move cancelled." }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await sendAndWaitForCard();

    fireEvent.click(screen.getByRole("button", { name: "Cancel moving APT-2001 to SCN-2" }));

    expect(await screen.findByText("Move cancelled.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cancel moving/ })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/sessions/sess-1/reschedule/7/cancel"),
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("disables the buttons while a request is in flight", async () => {
    let finish: (r: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chatPending: [PROPOSAL],
        resolve: () => new Promise<Response>((r) => (finish = r)) as unknown as Response,
      }),
    );
    await sendAndWaitForCard();

    fireEvent.click(screen.getByRole("button", { name: "Confirm moving APT-2001 to SCN-2" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Confirm moving APT-2001 to SCN-2" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Cancel moving APT-2001 to SCN-2" })).toBeDisabled();

    finish(jsonResponse({ status: "cancelled", message: "done" }));
    expect(await screen.findByText("done")).toBeInTheDocument();
  });

  it("shows a 409 detail inline and keeps the card", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chatPending: [PROPOSAL],
        resolve: () => errorResponse(409, "Scanner SCN-2 is no longer free."),
      }),
    );
    const onTurnComplete = await sendAndWaitForCard();
    onTurnComplete.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Confirm moving APT-2001 to SCN-2" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Scanner SCN-2 is no longer free.");
    expect(screen.getByRole("button", { name: "Confirm moving APT-2001 to SCN-2" })).toBeEnabled();
    expect(onTurnComplete).not.toHaveBeenCalled();
  });

  it("restores pending proposals from the server when an existing session is reloaded", async () => {
    sessionStorage.setItem("shoa_session_id", "sess-1");
    const fetchMock = createFetchMock({ listPending: [PROPOSAL] });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) =>
        String(url).endsWith("/messages") ? Promise.resolve(jsonResponse([])) : fetchMock(url, init),
      ),
    );

    render(<Harness onTurnComplete={vi.fn()} />);

    expect(await screen.findByText(/Move APT-2001 to SCN-2/)).toBeInTheDocument();
  });
});

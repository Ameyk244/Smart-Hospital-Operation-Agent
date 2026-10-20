import { useState } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { ChatPanel } from "./ChatPanel";
import type { TraceState } from "../hooks/useTrace";
import type { PendingBedAction } from "../api/types";

const emptyTrace: TraceState = { events: [], loading: false, error: null, hasFetched: false };

const ASSIGN: PendingBedAction = {
  proposal_id: 7,
  action: "assign_bed",
  bed_code: "BED-2",
  patient_code: "PT-1029",
};
const RELEASE: PendingBedAction = {
  proposal_id: 8,
  action: "release_bed",
  bed_code: "BED-1",
  patient_code: null,
};

function jsonResponse(data: unknown): Response {
  return { ok: true, json: () => Promise.resolve(data) } as Response;
}

function errorResponse(status: number, detail: string): Response {
  return { ok: false, status, statusText: "Error", json: () => Promise.resolve({ detail }) } as Response;
}

function createFetchMock(opts: {
  chatPending?: PendingBedAction[];
  listPending?: PendingBedAction[];
  resolve?: (action: "confirm" | "cancel") => Response;
  // Replies to chat posts after the first, consumed in order.
  laterReplies?: { handled_by: string; pending?: PendingBedAction[] }[];
}) {
  let chatCalls = 0;
  return vi.fn((url: string, init?: RequestInit) => {
    const u = String(url);
    if (u.includes("/api/chat") && init?.method === "POST") {
      if (opts.laterReplies && chatCalls++ > 0) {
        const { handled_by, pending } = opts.laterReplies.shift()!;
        return Promise.resolve(
          jsonResponse({
            session_id: "sess-1",
            handled_by,
            message: `reply via ${handled_by}`,
            data: null,
            terminated_reason: null,
            touched_entity_codes: [],
            pending_bed_actions: pending,
          }),
        );
      }
      return Promise.resolve(
        jsonResponse({
          session_id: "sess-1",
          handled_by: "agent",
          message: "Please confirm the bed change.",
          data: null,
          terminated_reason: null,
          touched_entity_codes: [],
          pending_bed_actions: opts.chatPending,
        }),
      );
    }
    if (u.endsWith("/api/sessions/sess-1/trace")) return Promise.resolve(jsonResponse([]));
    if (u.endsWith("/api/sessions/sess-1/pending-reschedules")) return Promise.resolve(jsonResponse([]));
    if (u.endsWith("/api/sessions/sess-1/pending-bed-actions")) {
      return Promise.resolve(jsonResponse(opts.listPending ?? []));
    }
    const action = u.match(/\/api\/sessions\/sess-1\/bed-actions\/7\/(confirm|cancel)$/);
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
  fireEvent.change(screen.getByPlaceholderText("Type a message…"), { target: { value: "admit PT-1029" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByText("Please confirm the bed change.");
  return onTurnComplete;
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  cleanup();
});

describe("BedActionConfirmation in the chat", () => {
  it("renders a card with Confirm and Cancel for each pending bed action", async () => {
    vi.stubGlobal("fetch", createFetchMock({ chatPending: [ASSIGN, RELEASE] }));
    await sendAndWaitForCard();

    expect(screen.getByRole("region", { name: "Pending bed actions" })).toBeInTheDocument();
    expect(screen.getByText("Admit PT-1029 to BED-2?")).toBeInTheDocument();
    expect(screen.getByText("Discharge the patient in BED-1 and send it for cleaning?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm admitting PT-1029 to BED-2" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Cancel discharging BED-1" })).toBeEnabled();
  });

  it("renders nothing when the response has no pending_bed_actions", async () => {
    vi.stubGlobal("fetch", createFetchMock({}));
    await sendAndWaitForCard();

    expect(screen.queryByRole("region", { name: "Pending bed actions" })).not.toBeInTheDocument();
  });

  it("keeps the card through a deterministic reply, and an agent reply with an empty list clears it", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chatPending: [ASSIGN],
        laterReplies: [{ handled_by: "deterministic", pending: [] }, { handled_by: "agent", pending: [] }],
      }),
    );
    await sendAndWaitForCard();
    const card = () => screen.queryByRole("button", { name: "Confirm admitting PT-1029 to BED-2" });

    fireEvent.change(screen.getByPlaceholderText("Type a message…"), { target: { value: "list wards" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("reply via deterministic");
    expect(card()).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("Type a message…"), { target: { value: "anything else" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("reply via agent");
    expect(card()).not.toBeInTheDocument();
  });

  it("Confirm posts the right ids, shows the message, removes the card and reports touched codes", async () => {
    const fetchMock = createFetchMock({
      chatPending: [ASSIGN],
      resolve: () =>
        jsonResponse({ status: "confirmed", message: "PT-1029 admitted to BED-2.", touched_entity_codes: ["PT-1029", "BED-2"] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const onTurnComplete = await sendAndWaitForCard();

    fireEvent.click(screen.getByRole("button", { name: "Confirm admitting PT-1029 to BED-2" }));

    expect(await screen.findByText("PT-1029 admitted to BED-2.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Confirm admitting/ })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/sessions/sess-1/bed-actions/7/confirm"),
      expect.objectContaining({ method: "POST" }),
    );
    expect(onTurnComplete).toHaveBeenLastCalledWith(["PT-1029", "BED-2"]);
  });

  it("Cancel posts the right ids, shows the message and removes the card", async () => {
    const fetchMock = createFetchMock({
      chatPending: [ASSIGN],
      resolve: () => jsonResponse({ status: "cancelled", message: "Admission cancelled." }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await sendAndWaitForCard();

    fireEvent.click(screen.getByRole("button", { name: "Cancel admitting PT-1029 to BED-2" }));

    expect(await screen.findByText("Admission cancelled.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cancel admitting/ })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/sessions/sess-1/bed-actions/7/cancel"),
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("disables the buttons while a request is in flight", async () => {
    let finish: (r: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chatPending: [ASSIGN],
        resolve: () => new Promise<Response>((r) => (finish = r)) as unknown as Response,
      }),
    );
    await sendAndWaitForCard();

    fireEvent.click(screen.getByRole("button", { name: "Confirm admitting PT-1029 to BED-2" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Confirm admitting PT-1029 to BED-2" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Cancel admitting PT-1029 to BED-2" })).toBeDisabled();

    finish(jsonResponse({ status: "cancelled", message: "done" }));
    expect(await screen.findByText("done")).toBeInTheDocument();
  });

  it("shows a 409 detail inline and keeps the card", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chatPending: [ASSIGN],
        resolve: () => errorResponse(409, "BED-2 is no longer free."),
      }),
    );
    const onTurnComplete = await sendAndWaitForCard();
    onTurnComplete.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Confirm admitting PT-1029 to BED-2" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("BED-2 is no longer free.");
    expect(screen.getByRole("button", { name: "Confirm admitting PT-1029 to BED-2" })).toBeEnabled();
    expect(onTurnComplete).not.toHaveBeenCalled();
  });

  it("restores pending bed actions from the server when an existing session is reloaded", async () => {
    sessionStorage.setItem("shoa_session_id", "sess-1");
    const fetchMock = createFetchMock({ listPending: [ASSIGN] });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) =>
        String(url).endsWith("/messages") ? Promise.resolve(jsonResponse([])) : fetchMock(url, init),
      ),
    );

    render(<Harness onTurnComplete={vi.fn()} />);

    expect(await screen.findByText("Admit PT-1029 to BED-2?")).toBeInTheDocument();
  });
});

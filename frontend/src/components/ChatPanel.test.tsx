import { useState } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { ChatPanel } from "./ChatPanel";
import type { TraceState } from "../hooks/useTrace";
import type { AgentEvent, ChatResponse, ConversationMessage } from "../api/types";

// Fresh, empty trace — most of these tests don't rely on the shared
// useTrace() hook's polling loop, only on ChatPanel's own direct
// api.getTrace() calls (see ChatPanel.tsx's handleSubmit and the progress
// indicator effect), so a static empty TraceState is enough here.
const emptyTrace: TraceState = { events: [], loading: false, error: null, hasFetched: false };

interface FetchOverrides {
  chat?: (body: { text: string; session_id?: string }) => Partial<ChatResponse>;
  trace?: Record<string, AgentEvent[]>;
  messages?: Record<string, ConversationMessage[]>;
}

function jsonResponse(data: unknown): Response {
  return { ok: true, json: () => Promise.resolve(data) } as Response;
}

// A router over the one thing every test needs: a fetch mock that answers
// POST /api/chat, GET /api/sessions/{id}/trace and GET
// /api/sessions/{id}/messages the way the real backend would, without ever
// making a real network call.
function createFetchMock(overrides: FetchOverrides = {}) {
  return vi.fn((url: string, init?: RequestInit) => {
    const u = String(url);

    if (u.includes("/api/chat") && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as { text: string; session_id?: string };
      const base: ChatResponse = {
        session_id: "sess-default",
        handled_by: "deterministic",
        message: "ok",
        data: null,
        terminated_reason: null,
        touched_entity_codes: [],
      };
      return Promise.resolve(jsonResponse({ ...base, ...(overrides.chat?.(body) ?? {}) }));
    }

    const traceMatch = u.match(/\/api\/sessions\/([^/]+)\/trace/);
    if (traceMatch) {
      const id = decodeURIComponent(traceMatch[1]);
      return Promise.resolve(jsonResponse(overrides.trace?.[id] ?? []));
    }

    const msgMatch = u.match(/\/api\/sessions\/([^/]+)\/messages/);
    if (msgMatch) {
      const id = decodeURIComponent(msgMatch[1]);
      return Promise.resolve(jsonResponse(overrides.messages?.[id] ?? []));
    }

    return Promise.reject(new Error(`Unhandled fetch in test: ${init?.method ?? "GET"} ${u}`));
  });
}

// Mirrors how App.tsx actually wires ChatPanel: sessionId is owned by the
// parent and fed back in via onSessionId. Needed for the tests that send a
// message and then simulate a reload — without this loop, ChatPanel's own
// `sessionId` prop would never reflect the session the first response
// created, same as it wouldn't in the real app if App.tsx didn't do this.
function Harness({ initialSessionId = null }: { initialSessionId?: string | null }) {
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  return (
    <ChatPanel
      sessionId={sessionId}
      onSessionId={setSessionId}
      onTurnComplete={() => {}}
      trace={emptyTrace}
    />
  );
}

function sendMessage(text: string) {
  fireEvent.change(screen.getByPlaceholderText("Type a message…"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  cleanup();
});

describe("ChatPanel session lifecycle (Task A)", () => {
  it("hydrates from the sessionStorage message cache on a same-tab reload, with no network call", async () => {
    sessionStorage.setItem("shoa_session_id", "sess-1");
    sessionStorage.setItem(
      "shoa_cached_messages",
      JSON.stringify({
        sessionId: "sess-1",
        messages: [
          { key: "u1", role: "user", content: "hello" },
          { key: "a1", role: "assistant", content: "hi there", handledBy: "agent" },
        ],
      }),
    );
    const fetchMock = createFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    const onSessionId = vi.fn();

    render(
      <ChatPanel sessionId={null} onSessionId={onSessionId} onTurnComplete={vi.fn()} trace={emptyTrace} />,
    );

    expect(await screen.findByText("hi there")).toBeInTheDocument();
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByText("agent")).toBeInTheDocument();
    expect(onSessionId).toHaveBeenCalledWith("sess-1");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("starts empty on a fresh tab even when localStorage's previous-sessions index is populated", () => {
    localStorage.setItem(
      "shoa_previous_sessions",
      JSON.stringify([
        { session_id: "sess-old", started_at: new Date().toISOString(), last_message_preview: "an old question" },
      ]),
    );
    const fetchMock = createFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(<ChatPanel sessionId={null} onSessionId={vi.fn()} onTurnComplete={vi.fn()} trace={emptyTrace} />);

    expect(screen.getByText(/Ask something like/)).toBeInTheDocument();
    expect(screen.getByText("Previous chats (1)")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("loads a previous session's history via GET /messages when selected from Previous chats", async () => {
    localStorage.setItem(
      "shoa_previous_sessions",
      JSON.stringify([
        { session_id: "sess-old", started_at: new Date().toISOString(), last_message_preview: "an old question" },
      ]),
    );
    const fetchMock = createFetchMock({
      messages: {
        "sess-old": [
          { role: "USER", content: "an old question", created_at: "2026-01-01T00:00:00Z" },
          { role: "ASSISTANT", content: "an old answer", created_at: "2026-01-01T00:00:01Z" },
        ],
      },
    });
    vi.stubGlobal("fetch", fetchMock);
    const onSessionId = vi.fn();

    render(
      <ChatPanel sessionId={null} onSessionId={onSessionId} onTurnComplete={vi.fn()} trace={emptyTrace} />,
    );

    fireEvent.click(screen.getByText("Previous chats (1)"));
    fireEvent.click(screen.getByText("an old question"));

    expect(await screen.findByText("an old answer")).toBeInTheDocument();
    expect(onSessionId).toHaveBeenCalledWith("sess-old");
    expect(sessionStorage.getItem("shoa_session_id")).toBe("sess-old");
  });

  it("New chat clears the active session pointer and cached messages, resetting the view", async () => {
    sessionStorage.setItem("shoa_session_id", "sess-1");
    sessionStorage.setItem(
      "shoa_cached_messages",
      JSON.stringify({ sessionId: "sess-1", messages: [{ key: "u1", role: "user", content: "hi" }] }),
    );
    vi.stubGlobal("fetch", createFetchMock());
    const onSessionId = vi.fn();

    render(
      <ChatPanel sessionId={null} onSessionId={onSessionId} onTurnComplete={vi.fn()} trace={emptyTrace} />,
    );

    expect(await screen.findByText("hi")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "New chat" }));

    expect(sessionStorage.getItem("shoa_session_id")).toBeNull();
    expect(sessionStorage.getItem("shoa_cached_messages")).toBeNull();
    expect(onSessionId).toHaveBeenCalledWith(null);
    expect(screen.getByText(/Ask something like/)).toBeInTheDocument();
  });

  it("a sent message is cached with its badge, and survives an unmount/remount (reload) with no new fetch for history", async () => {
    const fetchMock = createFetchMock({
      chat: () => ({ session_id: "sess-live", handled_by: "agent", message: "the answer", touched_entity_codes: [] }),
      trace: { "sess-live": [] },
    });
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(<Harness />);
    sendMessage("what scanners are free?");
    expect(await screen.findByText("the answer")).toBeInTheDocument();
    expect(screen.getByText("agent")).toBeInTheDocument();

    unmount();
    fetchMock.mockClear();

    // Simulate a same-tab reload: a brand new ChatPanel mount, reading
    // whatever got left behind in sessionStorage.
    render(<ChatPanel sessionId={null} onSessionId={vi.fn()} onTurnComplete={vi.fn()} trace={emptyTrace} />);

    expect(await screen.findByText("the answer")).toBeInTheDocument();
    expect(screen.getByText("agent")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("ChatPanel handled-by badge (Task B)", () => {
  it("shows the deterministic badge for a deterministic response", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({ chat: () => ({ session_id: "sess-a", handled_by: "deterministic", message: "3 departments" }) }),
    );
    render(<ChatPanel sessionId={null} onSessionId={vi.fn()} onTurnComplete={vi.fn()} trace={emptyTrace} />);

    sendMessage("list departments");

    expect(await screen.findByText("deterministic")).toBeInTheDocument();
  });

  it("shows the agent badge for an agent-routed response", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chat: () => ({ session_id: "sess-b", handled_by: "agent", message: "answer" }),
        trace: { "sess-b": [] },
      }),
    );
    render(<ChatPanel sessionId={null} onSessionId={vi.fn()} onTurnComplete={vi.fn()} trace={emptyTrace} />);

    sendMessage("what MRI scanners are free?");

    expect(await screen.findByText("agent")).toBeInTheDocument();
  });
});

describe("ChatPanel Markdown rendering (Task C)", () => {
  it("renders **bold** markdown as a <strong> element instead of literal asterisks", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chat: () => ({ session_id: "sess-md", handled_by: "deterministic", message: "Scanner **SCN-1** is free" }),
      }),
    );
    render(<ChatPanel sessionId={null} onSessionId={vi.fn()} onTurnComplete={vi.fn()} trace={emptyTrace} />);

    sendMessage("check scanner");

    const strong = await screen.findByText("SCN-1");
    expect(strong.tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*SCN-1\*\*/)).not.toBeInTheDocument();
  });

  it("renders a GFM table (pipe syntax) as real <table> markup", async () => {
    const tableMarkdown = "| Scanner | Status |\n| --- | --- |\n| SCN-1 | FREE |";
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chat: () => ({ session_id: "sess-tbl", handled_by: "deterministic", message: tableMarkdown }),
      }),
    );
    render(<ChatPanel sessionId={null} onSessionId={vi.fn()} onTurnComplete={vi.fn()} trace={emptyTrace} />);

    sendMessage("show table");

    const cell = await screen.findByText("FREE");
    expect(cell.tagName).toBe("TD");
    expect(cell.closest("table")).not.toBeNull();
  });
});

describe("ChatPanel agent progress indicator (Task D)", () => {
  it("collapses to a tool-call-count summary derived from the trace endpoint once an agent turn finishes", async () => {
    const fixtureEvents: AgentEvent[] = [
      {
        round_num: 1,
        event_type: "agent_invoked",
        tool_name: null,
        arguments_json: null,
        status: "SUCCESS",
        latency_ms: null,
        error_category: null,
        created_at: "t1",
      },
      {
        round_num: 1,
        event_type: "tool_requested",
        tool_name: "search_appointments",
        arguments_json: null,
        status: "SUCCESS",
        latency_ms: null,
        error_category: null,
        created_at: "t2",
      },
      {
        round_num: 1,
        event_type: "tool_executed",
        tool_name: "search_appointments",
        arguments_json: null,
        status: "SUCCESS",
        latency_ms: 120,
        error_category: null,
        created_at: "t3",
      },
      {
        round_num: 2,
        event_type: "tool_requested",
        tool_name: "list_scanners",
        arguments_json: null,
        status: "SUCCESS",
        latency_ms: null,
        error_category: null,
        created_at: "t4",
      },
      {
        round_num: 2,
        event_type: "tool_executed",
        tool_name: "list_scanners",
        arguments_json: null,
        status: "SUCCESS",
        latency_ms: 80,
        error_category: null,
        created_at: "t5",
      },
    ];
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chat: () => ({ session_id: "sess-agent", handled_by: "agent", message: "done" }),
        trace: { "sess-agent": fixtureEvents },
      }),
    );
    render(<ChatPanel sessionId={null} onSessionId={vi.fn()} onTurnComplete={vi.fn()} trace={emptyTrace} />);

    sendMessage("free MRI scanners for delayed appointments?");

    expect(await screen.findByText("✓ 2 tool calls")).toBeInTheDocument();
  });

  it("shows no collapsed tool-call summary for a deterministic (non-agent) response", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({ chat: () => ({ session_id: "sess-det", handled_by: "deterministic", message: "3 departments" }) }),
    );
    render(<ChatPanel sessionId={null} onSessionId={vi.fn()} onTurnComplete={vi.fn()} trace={emptyTrace} />);

    sendMessage("list departments");

    expect(await screen.findByText("deterministic")).toBeInTheDocument();
    expect(screen.queryByText(/tool call/)).not.toBeInTheDocument();
  });
});

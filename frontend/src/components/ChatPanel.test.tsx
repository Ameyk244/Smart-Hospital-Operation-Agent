import { useState } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup, act } from "@testing-library/react";
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

// A small controllable fake WebSocket, in the same spirit as
// createFetchMock above: lets a test open a "connection" (captured in
// .instances), capture what was sent, and inject server JSON text frames
// via serverMessage(). getUserMedia/AudioContext/AudioWorklet don't exist
// in jsdom and can't be meaningfully tested there — but pcmCapture.ts's
// startCapture() feature-detects navigator.mediaDevices.getUserMedia and
// cleanly no-ops when it's absent (exactly the jsdom case), and
// useVoiceInput only calls it from the socket's onopen handler, which none
// of these tests ever trigger — so the WS message-handling wiring below is
// fully exercisable without a real microphone.
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  readyState = 0;
  binaryType = "blob";
  url: string;
  sent: unknown[] = [];
  closeCalled = false;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: unknown) {
    this.sent.push(data);
  }

  close() {
    this.closeCalled = true;
    this.readyState = 3;
    this.onclose?.();
  }

  // Wrapped in act() since this synchronously drives React state updates
  // (setMessages, etc.) from outside any React event handler — the same
  // reason fireEvent wraps clicks/changes for you automatically.
  serverMessage(payload: unknown) {
    act(() => {
      this.onmessage?.({ data: JSON.stringify(payload) });
    });
  }
}

function startVoice() {
  fireEvent.click(screen.getByRole("button", { name: "Start voice input" }));
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

  // The typed-decision fast path: the parser missed, Jev mapped the
  // message onto a known command, and CommandRunner executed it with no
  // Claude call — so the turn reports itself as "jev", not "agent".
  it("shows the jev badge for a typed-decision fast-path response", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chat: () => ({
          session_id: "sess-jev",
          handled_by: "jev",
          message: "3 departments",
          touched_entity_codes: ["DEP-01"],
        }),
      }),
    );
    render(<ChatPanel sessionId={null} onSessionId={vi.fn()} onTurnComplete={vi.fn()} trace={emptyTrace} />);

    sendMessage("show me the departments please");

    expect(await screen.findByText("jev")).toBeInTheDocument();
    // Fast-path turns are not agent turns: no tool-call summary line.
    expect(screen.queryByText(/tool call/)).not.toBeInTheDocument();
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

describe("ChatPanel voice input", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  it("a full voice turn renders a user message then an assistant message with the right badge, in order", async () => {
    vi.stubGlobal("fetch", createFetchMock());
    const { container } = render(<Harness />);

    startVoice();
    const ws = FakeWebSocket.instances[0];
    expect(ws).toBeDefined();

    ws.serverMessage({ type: "transcript", text: "what scanners are free?" });
    expect(await screen.findByText("what scanners are free?")).toBeInTheDocument();

    ws.serverMessage({
      type: "chat_response",
      response: {
        session_id: "sess-voice-1",
        handled_by: "agent",
        message: "MRI-2 is free",
        data: null,
        terminated_reason: null,
        touched_entity_codes: ["MRI-2"],
      },
    });
    expect(await screen.findByText("MRI-2 is free")).toBeInTheDocument();
    expect(screen.getByText("agent")).toBeInTheDocument();

    // Checked via the outer bubble containers' textContent, not
    // getAllByText: the assistant bubble renders through ReactMarkdown,
    // which wraps its text in a nested <p>, so Testing Library's
    // innermost-match default would pick that <p> over the outer
    // ".chat-message-content" div a selector filter expects, while the
    // plain-text user bubble has no such wrapper — an asymmetry that made
    // one uniform selector-scoped query unreliable. querySelectorAll
    // sidesteps that by reading the outer bubbles directly, which also
    // avoids the *other* problem an unscoped query would have: the
    // brand-new-session bookkeeping below writes this same transcript text
    // into the "Previous chats" preview list too.
    const bubbles = container.querySelectorAll(".chat-message-content");
    expect(bubbles[0]).toHaveTextContent("what scanners are free?");
    expect(bubbles[1]).toHaveTextContent("MRI-2 is free");

    // Brand-new-session bookkeeping (handleSubmit's equivalent for voice).
    expect(sessionStorage.getItem("shoa_session_id")).toBe("sess-voice-1");
  });

  it("surfaces a server error event without crashing or losing existing chat history", async () => {
    vi.stubGlobal(
      "fetch",
      createFetchMock({
        chat: () => ({ session_id: "sess-x", handled_by: "deterministic", message: "3 departments" }),
      }),
    );
    render(<Harness />);

    sendMessage("list departments");
    expect(await screen.findByText("3 departments")).toBeInTheDocument();

    startVoice();
    const ws = FakeWebSocket.instances[0];
    ws.serverMessage({ type: "error", code: "empty_transcript", message: "Didn't catch that." });

    expect(await screen.findByText("Didn't catch that.")).toBeInTheDocument();
    // Earlier typed-turn history survives the error, unmodified. Scoped to
    // the message bubble itself: sending the first message of a new
    // session also writes this same text into the "Previous chats"
    // preview list, a second, unrelated match for an unscoped query.
    expect(screen.getByText("3 departments")).toBeInTheDocument();
    expect(
      screen.getByText("list departments", { selector: ".chat-message-content" }),
    ).toBeInTheDocument();
  });

  it("disables the mic button while a typed message is sending, and disables typed input while voice is active", async () => {
    let resolveChat: (overrides: Partial<ChatResponse>) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        const u = String(url);
        if (u.includes("/api/chat") && init?.method === "POST") {
          return new Promise<Response>((resolve) => {
            resolveChat = (overrides) =>
              resolve(
                jsonResponse({
                  session_id: "sess-y",
                  handled_by: "deterministic",
                  message: "ok",
                  data: null,
                  terminated_reason: null,
                  touched_entity_codes: [],
                  ...overrides,
                }),
              );
          });
        }
        return Promise.reject(new Error(`Unhandled fetch in test: ${init?.method ?? "GET"} ${u}`));
      }),
    );
    render(<Harness />);

    sendMessage("list departments");
    expect(screen.getByRole("button", { name: "Start voice input" })).toBeDisabled();

    resolveChat({});
    expect(await screen.findByText("ok")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start voice input" })).not.toBeDisabled();

    startVoice();
    expect(screen.getByPlaceholderText("Type a message…")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("closes the WebSocket on unmount, releasing the mic", () => {
    vi.stubGlobal("fetch", createFetchMock());
    const { unmount } = render(<Harness />);

    startVoice();
    const ws = FakeWebSocket.instances[0];
    expect(ws.closeCalled).toBe(false);

    unmount();
    expect(ws.closeCalled).toBe(true);
  });
});

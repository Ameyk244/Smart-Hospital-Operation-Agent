import { useState } from "react";
import "./App.css";
import { OperationsView } from "./components/OperationsView";
import { ChatPanel } from "./components/ChatPanel";
import { TracePanel } from "./components/TracePanel";

// Top-level layout: hospital operations browsing on the left, chat + trace
// stacked on the right. Both the chat and trace panels are always visible
// (no tab/toggle) per the project brief — the trace panel is a learning
// tool, not an optional debug drawer.
function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [traceRefreshToken, setTraceRefreshToken] = useState(0);

  return (
    <div className="app-shell">
      <header className="app-header">
        <h1>Smart Hospital Operations Agent</h1>
        <p className="muted">
          A window into a deterministic-parser + LangGraph-agent backend — not a hospital
          product. Left: read-only operations browsing. Right: chat, and the trace of exactly
          what the backend did to answer each message.
        </p>
      </header>
      <main className="app-main">
        <section className="app-column app-column-operations">
          <OperationsView />
        </section>
        <section className="app-column app-column-side">
          <div className="app-column-chat">
            <ChatPanel
              sessionId={sessionId}
              onSessionId={setSessionId}
              onTurnComplete={() => setTraceRefreshToken((t) => t + 1)}
            />
          </div>
          <div className="app-column-trace">
            <TracePanel sessionId={sessionId} refreshToken={traceRefreshToken} />
          </div>
        </section>
      </main>
    </div>
  );
}

export default App;

import { useState } from "react";
import "./App.css";
import { OperationsView } from "./components/OperationsView";
import { ChatPanel } from "./components/ChatPanel";
import { TracePanel } from "./components/TracePanel";
import { CostComparisonPage } from "./components/CostComparisonPage";
import { useTrace } from "./hooks/useTrace";

// There's no router in this project and this doesn't justify adding one:
// a single secondary page is a view switch, not a navigation graph.
// Switching to "cost" does unmount ChatPanel, but sessionId lives here,
// above the switch, and ChatPanel rehydrates its message list from the
// sessionStorage cache on the way back — so the round trip is invisible.
type View = "console" | "cost";

// Top-level layout: hospital operations browsing on the left, chat + trace
// stacked on the right. Both the chat and trace panels are always visible
// (no tab/toggle) per the project brief — the trace panel is a learning
// tool, not an optional debug drawer.
//
// Trace data is fetched once here (useTrace) and handed down to both
// TracePanel and ChatPanel (the latter for its compact in-chat progress
// indicator) — one fetch, two renderings, not two independent pollers
// against the same endpoint.
function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [traceRefreshToken, setTraceRefreshToken] = useState(0);
  const [touchedEntityCodes, setTouchedEntityCodes] = useState<string[]>([]);
  const [view, setView] = useState<View>("console");
  const trace = useTrace(sessionId, traceRefreshToken);

  function handleTurnComplete(codes: string[]) {
    setTraceRefreshToken((t) => t + 1);
    setTouchedEntityCodes(codes);
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header-row">
          <h1>Smart Hospital Operations Agent</h1>
          {view === "console" && (
            <button type="button" className="chat-new-button" onClick={() => setView("cost")}>
              Cost comparison
            </button>
          )}
        </div>
        <p className="muted">
          A window into a deterministic-parser + LangGraph-agent backend — not a hospital
          product. Left: read-only operations browsing. Right: chat, and the trace of exactly
          what the backend did to answer each message.
        </p>
      </header>
      {view === "cost" ? (
        <main className="app-main app-main-single">
          <CostComparisonPage onBack={() => setView("console")} />
        </main>
      ) : (
        <main className="app-main">
          <section className="app-column app-column-operations">
            <OperationsView touchedEntityCodes={touchedEntityCodes} />
          </section>
          <section className="app-column app-column-side">
            <div className="app-column-chat">
              <ChatPanel
                sessionId={sessionId}
                onSessionId={setSessionId}
                onTurnComplete={handleTurnComplete}
                trace={trace}
              />
            </div>
            <div className="app-column-trace">
              <TracePanel sessionId={sessionId} trace={trace} />
            </div>
          </section>
        </main>
      )}
    </div>
  );
}

export default App;

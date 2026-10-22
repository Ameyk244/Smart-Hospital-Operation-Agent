import { useState, type ReactNode } from "react";
import "./App.css";
import { OperationsView } from "./components/OperationsView";
import { ChatPanel } from "./components/ChatPanel";
import { TracePanel } from "./components/TracePanel";
import { CostComparisonPage } from "./components/CostComparisonPage";
import { StatusLights } from "./components/StatusLights";
import { WardsPage } from "./components/WardsPage";
import { EmergencyQueuePage } from "./components/EmergencyQueuePage";
import { JourneyPage } from "./components/JourneyPage";
import { useTrace } from "./hooks/useTrace";
import { AlertBell } from "./components/AlertBell";
import { AlertsPage } from "./components/AlertsPage";
import { useStatus } from "./hooks/useStatus";
import { useOpenAlerts } from "./hooks/useOpenAlerts";

// There's no router in this project and this doesn't justify adding one:
// a handful of pages is a view switch, not a navigation graph.
// The chat and trace column sits outside the switch, so it never remounts
// when the page changes.
type View = "console" | "wards" | "queue" | "journey" | "alerts" | "cost";

const icon = (paths: ReactNode) => (
  <svg
    className="sidebar-icon"
    viewBox="0 0 24 24"
    width="18"
    height="18"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    {paths}
  </svg>
);

const NAV_ITEMS: { view: View; label: string; icon: ReactNode }[] = [
  {
    view: "console",
    label: "Operations",
    icon: icon(<path d="M3 12h4l3-8 4 16 3-8h4" />),
  },
  {
    view: "wards",
    label: "Wards",
    icon: icon(<path d="M3 20V8m18 12v-6a3 3 0 0 0-3-3H3m0 5h18M7 11V9" />),
  },
  {
    view: "queue",
    label: "Emergency queue",
    icon: icon(
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </>,
    ),
  },
  {
    view: "journey",
    label: "Patient journey",
    icon: icon(
      <>
        <circle cx="5" cy="18" r="2" />
        <circle cx="19" cy="6" r="2" />
        <path d="M7 18h4a4 4 0 0 0 4-4v-4a4 4 0 0 1 2-3" />
      </>,
    ),
  },
  {
    view: "alerts",
    label: "Alerts",
    icon: icon(<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0" />),
  },
  {
    view: "cost",
    label: "Cost comparison",
    icon: icon(<path d="M12 2v20M17 6H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />),
  },
];

// Top-level layout: sidebar navigation, the selected page in the middle, chat
// + trace stacked on the right. Both the chat and trace panels are always
// visible (no tab/toggle) per the project brief — the trace panel is a
// learning tool, not an optional debug drawer.
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
  const status = useStatus();
  const schedulerOn = status.status?.scheduler?.status === "on";
  const openAlerts = useOpenAlerts(schedulerOn);

  function handleTurnComplete(codes: string[]) {
    setTraceRefreshToken((t) => t + 1);
    setTouchedEntityCodes(codes);
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header-row">
          <h1>Smart Hospital Operations Agent</h1>
          <div className="app-header-actions">
            <AlertBell
              schedulerOn={schedulerOn}
              count={openAlerts.length}
              onOpen={() => setView("alerts")}
            />
            <StatusLights state={status} />
          </div>
        </div>
        <p className="muted">
          A window into a deterministic-parser + LangGraph-agent backend — not a hospital
          product. Left: read-only operations browsing. Right: chat, and the trace of exactly
          what the backend did to answer each message.
        </p>
      </header>
      <div className="app-body">
        <nav className="sidebar" aria-label="Main navigation">
          <ul className="sidebar-list">
            {NAV_ITEMS.filter((item) => item.view !== "alerts" || schedulerOn).map((item) => (
              <li key={item.view}>
                <button
                  type="button"
                  className={`sidebar-item${view === item.view ? " sidebar-item-active" : ""}`}
                  aria-current={view === item.view ? "page" : undefined}
                  title={item.label}
                  onClick={() => setView(item.view)}
                >
                  {item.icon}
                  <span className="sidebar-label">{item.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <main className="app-main">
          <section className="app-column app-column-operations">
            {view === "console" && <OperationsView touchedEntityCodes={touchedEntityCodes} />}
            {view === "wards" && <WardsPage touchedEntityCodes={touchedEntityCodes} />}
            {view === "queue" && <EmergencyQueuePage touchedEntityCodes={touchedEntityCodes} />}
            {view === "journey" && <JourneyPage touchedEntityCodes={touchedEntityCodes} />}
            {view === "alerts" && <AlertsPage alerts={openAlerts} />}
            {view === "cost" && <CostComparisonPage onBack={() => setView("console")} />}
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
      </div>
    </div>
  );
}

export default App;

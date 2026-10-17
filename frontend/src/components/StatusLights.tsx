import type { Light } from "../api/types";
import type { StatusState } from "../hooks/useStatus";

type Display = Light | "unknown";

function StatusLight({ label, state, detail }: { label: string; state: Display; detail?: string }) {
  return (
    <li
      className={`status-light status-light-${state}`}
      title={`${label}: ${state}${detail ? ` (${detail})` : ""}`}
    >
      <span className="status-light-dot" aria-hidden="true" />
      {label}
      <span className="sr-only">: {state}</span>
    </li>
  );
}

// Three lights for the parts of the backend that can be on, off or down.
// When the status fetch itself fails (or hasn't landed yet) every light is
// "unknown": the last known values can't be trusted, and a stale green
// would be worse than no answer.
export function StatusLights({ state }: { state: StatusState }) {
  const status = state.unreachable ? null : state.status;
  return (
    <ul className="status-lights" aria-label="System status">
      <StatusLight
        label="Agent"
        state={status?.agent.status ?? "unknown"}
        detail={status ? (status.agent.runtime === "harness" ? "harness runtime" : "in-process runtime") : undefined}
      />
      <StatusLight label="Harness" state={status?.harness.status ?? "unknown"} />
      <StatusLight label="Jev" state={status?.jev.status ?? "unknown"} />
    </ul>
  );
}

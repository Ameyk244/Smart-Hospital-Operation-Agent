import { DepartmentsPanel } from "./DepartmentsPanel";
import { ScannersPanel } from "./ScannersPanel";
import { AppointmentsPanel } from "./AppointmentsPanel";
import { PatientSearchPanel } from "./PatientSearchPanel";

// Read-only browsing over /api/operations/*. No relation to the chat panel
// beyond a user possibly looking something up here before asking the agent
// about it (per the brief) — each sub-panel manages its own fetch/filters.
export function OperationsView() {
  return (
    <div className="operations-view">
      <h2>Hospital operations</h2>
      <DepartmentsPanel />
      <ScannersPanel />
      <AppointmentsPanel />
      <PatientSearchPanel />
    </div>
  );
}

import { DepartmentsPanel } from "./DepartmentsPanel";
import { ScannersPanel } from "./ScannersPanel";
import { AppointmentsPanel } from "./AppointmentsPanel";
import { PatientSearchPanel } from "./PatientSearchPanel";

interface OperationsViewProps {
  // Entity codes the most recent chat turn touched (from ChatResponse.
  // touched_entity_codes) — each sub-panel briefly highlights whichever of
  // its own rows match. No relation to the chat panel beyond that; a user
  // may also just look something up here before asking the agent about it.
  touchedEntityCodes: string[];
}

export function OperationsView({ touchedEntityCodes }: OperationsViewProps) {
  return (
    <div className="operations-view">
      <h2>Hospital operations</h2>
      <DepartmentsPanel touchedEntityCodes={touchedEntityCodes} />
      <ScannersPanel touchedEntityCodes={touchedEntityCodes} />
      <AppointmentsPanel touchedEntityCodes={touchedEntityCodes} />
      <PatientSearchPanel touchedEntityCodes={touchedEntityCodes} />
    </div>
  );
}

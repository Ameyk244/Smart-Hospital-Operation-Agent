import { useState } from "react";
import { api } from "../api/client";
import type {
  CancelBedActionResponse,
  ConfirmBedActionResponse,
  PendingBedAction,
} from "../api/types";

interface BedActionConfirmationProps {
  sessionId: string;
  proposals: PendingBedAction[];
  onResolved: (
    proposalId: number,
    result: ConfirmBedActionResponse | CancelBedActionResponse,
  ) => void;
}

function describe(p: PendingBedAction): { text: string; target: string } {
  if (p.action === "assign_bed") {
    const patient = p.patient_code ?? "the patient";
    return { text: `Admit ${patient} to ${p.bed_code}?`, target: `admitting ${patient} to ${p.bed_code}` };
  }
  return {
    text: `Discharge the patient in ${p.bed_code} and send it for cleaning?`,
    target: `discharging ${p.bed_code}`,
  };
}

export function BedActionConfirmation({ sessionId, proposals, onResolved }: BedActionConfirmationProps) {
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Record<number, string>>({});

  if (proposals.length === 0) return null;

  async function resolve(proposalId: number, action: "confirm" | "cancel") {
    setBusy(true);
    setErrors((prev) => {
      const next = { ...prev };
      delete next[proposalId];
      return next;
    });
    try {
      const result =
        action === "confirm"
          ? await api.confirmBedAction(sessionId, proposalId)
          : await api.cancelBedAction(sessionId, proposalId);
      onResolved(proposalId, result);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setErrors((prev) => ({ ...prev, [proposalId]: message }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="reschedule-confirmation" aria-label="Pending bed actions">
      {proposals.map((p) => {
        const { text, target } = describe(p);
        return (
          <div key={p.proposal_id} className="reschedule-card">
            <p className="reschedule-card-text">{text}</p>
            <div className="reschedule-card-actions">
              <button
                type="button"
                className="reschedule-confirm"
                disabled={busy}
                aria-label={`Confirm ${target}`}
                onClick={() => resolve(p.proposal_id, "confirm")}
              >
                Confirm
              </button>
              <button
                type="button"
                disabled={busy}
                aria-label={`Cancel ${target}`}
                onClick={() => resolve(p.proposal_id, "cancel")}
              >
                Cancel
              </button>
            </div>
            {errors[p.proposal_id] && (
              <p className="error" role="alert">
                {errors[p.proposal_id]}
              </p>
            )}
          </div>
        );
      })}
    </section>
  );
}

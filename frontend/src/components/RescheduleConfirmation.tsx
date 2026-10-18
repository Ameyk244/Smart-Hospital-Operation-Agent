import { useState } from "react";
import { api } from "../api/client";
import type {
  CancelRescheduleResponse,
  ConfirmRescheduleResponse,
  PendingReschedule,
} from "../api/types";

interface RescheduleConfirmationProps {
  sessionId: string;
  proposals: PendingReschedule[];
  onResolved: (
    proposalId: number,
    result: ConfirmRescheduleResponse | CancelRescheduleResponse,
  ) => void;
}

export function RescheduleConfirmation({ sessionId, proposals, onResolved }: RescheduleConfirmationProps) {
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
          ? await api.confirmReschedule(sessionId, proposalId)
          : await api.cancelReschedule(sessionId, proposalId);
      onResolved(proposalId, result);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setErrors((prev) => ({ ...prev, [proposalId]: message }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="reschedule-confirmation" aria-label="Pending appointment moves">
      {proposals.map((p) => {
        const target = `${p.appointment_code} to ${p.scanner_code}`;
        return (
          <div key={p.proposal_id} className="reschedule-card">
            <p className="reschedule-card-text">
              Move {target}
              {p.new_start && ` (new time ${new Date(p.new_start).toLocaleString()})`}
            </p>
            <div className="reschedule-card-actions">
              <button
                type="button"
                className="reschedule-confirm"
                disabled={busy}
                aria-label={`Confirm moving ${target}`}
                onClick={() => resolve(p.proposal_id, "confirm")}
              >
                Confirm
              </button>
              <button
                type="button"
                disabled={busy}
                aria-label={`Cancel moving ${target}`}
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

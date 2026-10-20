import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { useRowHighlight } from "../hooks/useRowHighlight";
import type { QueueEntry } from "../api/types";
import "./pages.css";

const URGENCIES = ["", "CRITICAL", "URGENT", "STANDARD"] as const;
const STATUSES = ["", "WAITING", "ADMITTED", "DISCHARGED"] as const;
const URGENCY_RANK: Record<string, number> = { CRITICAL: 0, URGENT: 1, STANDARD: 2 };
const REFRESH_MS = 30_000;

function formatWait(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

interface EmergencyQueuePageProps {
  touchedEntityCodes: string[];
}

export function EmergencyQueuePage({ touchedEntityCodes }: EmergencyQueuePageProps) {
  const [urgency, setUrgency] = useState<string>("");
  const [status, setStatus] = useState<string>("");
  const [entries, setEntries] = useState<QueueEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    function load(initial: boolean) {
      if (initial) {
        setLoading(true);
        setError(null);
      }
      api
        .listQueue({ urgency: urgency || undefined, status: status || undefined })
        .then((data) => {
          if (cancelled) return;
          setEntries(data);
          setError(null);
        })
        .catch((err) => {
          if (!cancelled) setError(String(err));
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }
    load(true);
    const timer = setInterval(() => load(false), REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [urgency, status]);

  const sorted = useMemo(
    () =>
      [...entries].sort(
        (a, b) =>
          (URGENCY_RANK[a.urgency] ?? 99) - (URGENCY_RANK[b.urgency] ?? 99) ||
          b.wait_minutes - a.wait_minutes,
      ),
    [entries],
  );

  const { rowRef } = useRowHighlight(sorted, (e) => e.code, touchedEntityCodes);

  return (
    <section className="panel-section">
      <h3>Emergency queue</h3>
      <div className="filter-row">
        <label>
          Urgency
          <select value={urgency} onChange={(e) => setUrgency(e.target.value)}>
            {URGENCIES.map((u) => (
              <option key={u} value={u}>
                {u || "All"}
              </option>
            ))}
          </select>
        </label>
        <label>
          Status
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s || "All"}
              </option>
            ))}
          </select>
        </label>
      </div>
      {loading && <p className="muted">Loading…</p>}
      {error && <p className="error">{error}</p>}
      {!loading && (
        <div className="pg-table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Entry</th>
                <th>Patient</th>
                <th>Urgency</th>
                <th>Status</th>
                <th>Wait</th>
                <th>Bed</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((e) => (
                <tr
                  key={e.code}
                  ref={rowRef(e.code)}
                  className={touchedEntityCodes.includes(e.code) ? "row-touched" : undefined}
                >
                  <td>{e.code}</td>
                  <td>{e.patient.name}</td>
                  <td>
                    <span className={`pg-badge pg-badge-${e.urgency.toLowerCase()}`}>
                      {e.urgency}
                    </span>
                  </td>
                  <td>
                    <span className={`pg-badge pg-badge-${e.status.toLowerCase()}`}>
                      {e.status}
                    </span>
                  </td>
                  <td className="pg-wait">{formatWait(e.wait_minutes)}</td>
                  <td>{e.bed ? `${e.bed.code} (${e.bed.ward.name})` : "—"}</td>
                </tr>
              ))}
              {sorted.length === 0 && !error && (
                <tr>
                  <td colSpan={6} className="muted">
                    No queue entries match these filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

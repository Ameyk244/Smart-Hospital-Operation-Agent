import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Scanner } from "../api/types";

const TYPES = ["", "MRI", "CT", "XRAY"] as const;
const STATUSES = ["", "AVAILABLE", "IN_USE", "MAINTENANCE"] as const;

export function ScannersPanel() {
  const [type, setType] = useState<string>("");
  const [status, setStatus] = useState<string>("");
  const [scanners, setScanners] = useState<Scanner[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listScanners({ type: type || undefined, status: status || undefined })
      .then((data) => {
        if (!cancelled) setScanners(data);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [type, status]);

  return (
    <section className="panel-section">
      <h3>Scanners</h3>
      <div className="filter-row">
        <label>
          Type
          <select value={type} onChange={(e) => setType(e.target.value)}>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t || "All"}
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
      {!loading && !error && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Name</th>
              <th>Type</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {scanners.map((s) => (
              <tr key={s.code}>
                <td>{s.code}</td>
                <td>{s.name}</td>
                <td>{s.type}</td>
                <td>
                  <span className={`status-badge status-${s.status.toLowerCase()}`}>
                    {s.status}
                  </span>
                </td>
              </tr>
            ))}
            {scanners.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  No scanners match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </section>
  );
}

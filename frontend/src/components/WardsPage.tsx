import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useRowHighlight } from "../hooks/useRowHighlight";
import type { Bed, Ward } from "../api/types";
import "./pages.css";

const STATUSES = ["", "AVAILABLE", "OCCUPIED", "CLEANING"] as const;

interface WardsPageProps {
  touchedEntityCodes: string[];
}

export function WardsPage({ touchedEntityCodes }: WardsPageProps) {
  const [status, setStatus] = useState<string>("");
  const [wards, setWards] = useState<Ward[]>([]);
  const [beds, setBeds] = useState<Bed[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([api.listWards(), api.listBeds({ status: status || undefined })])
      .then(([wardData, bedData]) => {
        if (cancelled) return;
        setWards(wardData);
        setBeds(bedData);
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
  }, [status]);

  const { rowRef } = useRowHighlight(beds, (b) => b.code, touchedEntityCodes);

  return (
    <section className="panel-section">
      <h3>Wards</h3>
      <div className="filter-row">
        <label>
          Bed status
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
      {!loading && !error && wards.length === 0 && (
        <p className="muted pg-empty">No wards found.</p>
      )}
      {!loading && !error && wards.length > 0 && (
        <div className="pg-ward-list">
          {wards.map((ward) => {
            const wardBeds = beds.filter((b) => b.ward.code === ward.code);
            const pct = ward.bed_count > 0 ? (ward.occupied_count / ward.bed_count) * 100 : 0;
            return (
              <article
                key={ward.code}
                className={`pg-ward-card${touchedEntityCodes.includes(ward.code) ? " row-touched" : ""}`}
              >
                <h4>{ward.name}</h4>
                <div className="pg-ward-meta">
                  <span>
                    {ward.code} · {ward.department.name}
                  </span>
                  <span>
                    {ward.occupied_count}/{ward.bed_count} occupied
                  </span>
                </div>
                <div
                  className="pg-occupancy"
                  role="progressbar"
                  aria-label={`${ward.name} occupancy`}
                  aria-valuemin={0}
                  aria-valuemax={ward.bed_count}
                  aria-valuenow={ward.occupied_count}
                >
                  <div className="pg-occupancy-fill" style={{ width: `${pct}%` }} />
                </div>
                {wardBeds.length === 0 ? (
                  <p className="muted">No beds match this filter.</p>
                ) : (
                  <table className="pg-bed-grid">
                    <tbody>
                      {wardBeds.map((b) => (
                        <tr
                          key={b.code}
                          ref={rowRef(b.code)}
                          className={touchedEntityCodes.includes(b.code) ? "row-touched" : undefined}
                        >
                          <td className="pg-bed-code">{b.code}</td>
                          <td>
                            <span className={`pg-badge pg-badge-${b.status.toLowerCase()}`}>
                              {b.status}
                            </span>
                          </td>
                          <td className="muted">{b.patient?.name ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

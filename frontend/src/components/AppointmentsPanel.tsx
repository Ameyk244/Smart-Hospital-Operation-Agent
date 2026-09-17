import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useRowHighlight } from "../hooks/useRowHighlight";
import type { Appointment } from "../api/types";

const STATUSES = ["", "SCHEDULED", "DELAYED", "COMPLETED", "CANCELLED"] as const;
const TYPES = ["", "MRI", "CT", "XRAY"] as const;

function formatDateTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

interface AppointmentsPanelProps {
  touchedEntityCodes: string[];
}

export function AppointmentsPanel({ touchedEntityCodes }: AppointmentsPanelProps) {
  const [status, setStatus] = useState<string>("");
  const [appointmentType, setAppointmentType] = useState<string>("");
  const [patientCode, setPatientCode] = useState<string>("");
  const [scannerCode, setScannerCode] = useState<string>("");
  const [appointments, setAppointments] = useState<Appointment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const handle = setTimeout(() => {
      api
        .searchAppointments({
          status: status || undefined,
          appointment_type: appointmentType || undefined,
          patient_code: patientCode || undefined,
          scanner_code: scannerCode || undefined,
          limit: 50,
        })
        .then((data) => {
          if (!cancelled) setAppointments(data);
        })
        .catch((err) => {
          if (!cancelled) setError(String(err));
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, 250); // debounce free-text filters
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [status, appointmentType, patientCode, scannerCode]);

  const { rowRef } = useRowHighlight(appointments, (a) => a.code, touchedEntityCodes);

  return (
    <section className="panel-section">
      <h3>Appointments</h3>
      <div className="filter-row">
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
        <label>
          Type
          <select value={appointmentType} onChange={(e) => setAppointmentType(e.target.value)}>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t || "All"}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="preset-button"
          onClick={() => {
            setStatus("DELAYED");
            setAppointmentType("MRI");
          }}
          title="Shortcut: status=DELAYED, type=MRI"
        >
          Delayed MRI
        </button>
      </div>
      <div className="filter-row">
        <label>
          Patient code
          <input
            type="text"
            value={patientCode}
            placeholder="e.g. PT-014"
            onChange={(e) => setPatientCode(e.target.value)}
          />
        </label>
        <label>
          Scanner code
          <input
            type="text"
            value={scannerCode}
            placeholder="e.g. SCN-03"
            onChange={(e) => setScannerCode(e.target.value)}
          />
        </label>
      </div>
      {loading && <p className="muted">Loading…</p>}
      {error && <p className="error">{error}</p>}
      {!loading && !error && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Patient</th>
              <th>Department</th>
              <th>Scanner</th>
              <th>Type</th>
              <th>Start</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {appointments.map((a) => (
              <tr
                key={a.code}
                ref={rowRef(a.code)}
                className={touchedEntityCodes.includes(a.code) ? "row-touched" : undefined}
              >
                <td>{a.code}</td>
                <td>{a.patient.name}</td>
                <td>{a.department.name}</td>
                <td>{a.scanner ? a.scanner.code : "—"}</td>
                <td>{a.appointment_type}</td>
                <td>{formatDateTime(a.scheduled_start)}</td>
                <td>
                  <span className={`status-badge status-${a.status.toLowerCase()}`}>
                    {a.status}
                  </span>
                </td>
              </tr>
            ))}
            {appointments.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  No appointments match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </section>
  );
}

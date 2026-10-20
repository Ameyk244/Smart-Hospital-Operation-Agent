import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api/client";
import { useRowHighlight } from "../hooks/useRowHighlight";
import type { Journey, Patient } from "../api/types";
import "./pages.css";

interface JourneyPageProps {
  touchedEntityCodes: string[];
}

export function JourneyPage({ touchedEntityCodes }: JourneyPageProps) {
  const [query, setQuery] = useState("");
  const [patients, setPatients] = useState<Patient[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [journey, setJourney] = useState<Journey | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const appointments = journey?.appointments ?? [];
  const { rowRef } = useRowHighlight(appointments, (a) => a.code, touchedEntityCodes);

  function search(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setSearching(true);
    setError(null);
    api
      .searchPatients(q)
      .then(setPatients)
      .catch((err) => setError(String(err)))
      .finally(() => setSearching(false));
  }

  function choose(code: string) {
    setSelected(code);
    setLoading(true);
    setError(null);
    api
      .getJourney(code)
      .then(setJourney)
      .catch((err) => {
        setJourney(null);
        setError(String(err));
      })
      .finally(() => setLoading(false));
  }

  return (
    <section className="panel-section">
      <h3>Patient journey</h3>
      <form className="filter-row" onSubmit={search}>
        <label>
          Patient name
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search patients"
          />
        </label>
        <button type="submit" disabled={searching || !query.trim()}>
          Search
        </button>
      </form>
      {searching && <p className="muted">Searching…</p>}
      {error && <p className="error">{error}</p>}
      {patients && patients.length === 0 && !searching && (
        <p className="muted">No patients match this search.</p>
      )}
      {patients && patients.length > 0 && (
        <ul className="pg-search-results">
          {patients.map((p) => (
            <li key={p.code}>
              <button type="button" aria-pressed={p.code === selected} onClick={() => choose(p.code)}>
                {p.name} ({p.code})
              </button>
            </li>
          ))}
        </ul>
      )}
      {!selected && !patients && <p className="muted pg-empty">Search for a patient to see their journey.</p>}
      {loading && <p className="muted">Loading…</p>}
      {journey && !loading && (
        <>
          <h4>
            {journey.patient.name} ({journey.patient.code})
          </h4>
          <ol className="pg-stepper" aria-label="Journey stages">
            {journey.stages.map((s) => {
              const classes = ["pg-step"];
              if (s.at) classes.push("pg-step-done");
              if (s.stage === journey.current_stage) classes.push("pg-step-current");
              return (
                <li
                  key={s.stage}
                  className={classes.join(" ")}
                  aria-current={s.stage === journey.current_stage ? "step" : undefined}
                >
                  <span className="pg-step-name">{s.stage}</span>
                  <span className="pg-step-time">
                    {s.at ? new Date(s.at).toLocaleString() : "Not reached"}
                  </span>
                  {s.detail && <span className="pg-step-detail">{s.detail}</span>}
                </li>
              );
            })}
          </ol>
          <h4>Appointments</h4>
          <div className="pg-table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Code</th>
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
                    <td>{a.appointment_type}</td>
                    <td>{new Date(a.scheduled_start).toLocaleString()}</td>
                    <td>
                      <span className={`status-badge status-${a.status.toLowerCase()}`}>
                        {a.status}
                      </span>
                    </td>
                  </tr>
                ))}
                {appointments.length === 0 && (
                  <tr>
                    <td colSpan={4} className="muted">
                      No appointments for this patient.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api/client";
import type { Patient } from "../api/types";

export function PatientSearchPanel() {
  const [query, setQuery] = useState("");
  const [patients, setPatients] = useState<Patient[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    api
      .searchPatients(trimmed)
      .then((data) => {
        setPatients(data);
        setSearched(true);
      })
      .catch((err) => setError(String(err)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="panel-section">
      <h3>Patient search</h3>
      <form className="filter-row" onSubmit={handleSubmit}>
        <label>
          Name
          <input
            type="text"
            value={query}
            placeholder="e.g. Nguyen"
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <button type="submit" disabled={!query.trim() || loading}>
          Search
        </button>
      </form>
      {loading && <p className="muted">Searching…</p>}
      {error && <p className="error">{error}</p>}
      {!loading && !error && searched && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>MRN</th>
              <th>Name</th>
              <th>Date of birth</th>
            </tr>
          </thead>
          <tbody>
            {patients.map((p) => (
              <tr key={p.code}>
                <td>{p.code}</td>
                <td>{p.mrn}</td>
                <td>{p.name}</td>
                <td>{p.date_of_birth}</td>
              </tr>
            ))}
            {patients.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  No patients matched "{query}".
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </section>
  );
}

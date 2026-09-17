import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useRowHighlight } from "../hooks/useRowHighlight";
import type { Department } from "../api/types";

interface DepartmentsPanelProps {
  touchedEntityCodes: string[];
}

export function DepartmentsPanel({ touchedEntityCodes }: DepartmentsPanelProps) {
  const [departments, setDepartments] = useState<Department[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .listDepartments()
      .then((data) => {
        if (!cancelled) setDepartments(data);
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
  }, []);

  const { rowRef } = useRowHighlight(departments, (d) => d.code, touchedEntityCodes);

  return (
    <section className="panel-section">
      <h3>Departments</h3>
      {loading && <p className="muted">Loading…</p>}
      {error && <p className="error">{error}</p>}
      {!loading && !error && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Name</th>
            </tr>
          </thead>
          <tbody>
            {departments.map((d) => (
              <tr
                key={d.code}
                ref={rowRef(d.code)}
                className={touchedEntityCodes.includes(d.code) ? "row-touched" : undefined}
              >
                <td>{d.code}</td>
                <td>{d.name}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

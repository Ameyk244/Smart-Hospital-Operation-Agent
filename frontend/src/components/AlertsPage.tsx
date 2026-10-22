import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Alert, ScheduledTask, WatchCondition } from "../api/types";
import "./pages.css";

const REFRESH_MS = 30_000;

const CONDITION_WORDS: Record<WatchCondition, string> = {
  QUEUE_WAIT_OVER: "Emergency queue wait over {n} minutes",
  FREE_BEDS_UNDER: "Free beds under {n}",
  DELAYED_APPOINTMENTS_OVER: "Delayed appointments over {n}",
};

function relativeTime(iso: string): string {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} h ago`;
  return `${Math.floor(minutes / 1440)} d ago`;
}

function formatPeriod(seconds: number): string {
  return seconds % 60 === 0 ? `every ${seconds / 60} min` : `every ${seconds} s`;
}

export function AlertsPage({ alerts }: { alerts: Alert[] }) {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    function load() {
      api
        .listScheduledTasks()
        .then((data) => {
          if (cancelled) return;
          setTasks(data);
          setError(null);
        })
        .catch((err) => {
          if (!cancelled) setError(String(err));
        });
    }
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const watches = tasks.filter((t) => t.active);

  return (
    <section className="panel-section">
      <h3>Alerts</h3>
      <p className="muted">
        Read-only. Acknowledge alerts and create or stop watches in chat, for example
        &ldquo;acknowledge alert 5&rdquo;.
      </p>
      {error && <p className="error">{error}</p>}

      <h4>Open alerts</h4>
      {alerts.length === 0 ? (
        <p className="pg-empty muted">
          No open alerts. Create a watch in chat, for example &ldquo;watch queue wait over 30
          minutes&rdquo;.
        </p>
      ) : (
        <ul className="pg-alert-list">
          {alerts.map((a) => (
            <li key={a.id} className="pg-alert">
              <span className={`pg-badge pg-badge-${a.severity.toLowerCase()}`}>{a.severity}</span>
              <span className="pg-alert-summary">{a.summary}</span>
              <span className="pg-alert-meta">
                {a.entity_codes.join(", ")} &middot; {relativeTime(a.created_at)}
              </span>
            </li>
          ))}
        </ul>
      )}

      <h4>Active watches</h4>
      {watches.length === 0 ? (
        <p className="pg-empty muted">
          No active watches. Create one in chat, for example &ldquo;watch queue wait over 30
          minutes&rdquo;.
        </p>
      ) : (
        <div className="pg-table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Watch</th>
                <th>Condition</th>
                <th>Scope</th>
                <th>Period</th>
                <th>Last run</th>
              </tr>
            </thead>
            <tbody>
              {watches.map((t) => (
                <tr key={t.code}>
                  <td>{t.code}</td>
                  <td>{(CONDITION_WORDS[t.condition] ?? t.condition).replace("{n}", String(t.threshold))}</td>
                  <td>{t.scope_code ?? "Whole hospital"}</td>
                  <td>{formatPeriod(t.period_seconds)}</td>
                  <td>{t.last_run_at ? relativeTime(t.last_run_at) : "never"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

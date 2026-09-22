import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { CostComparison } from "../api/types";

// Standalone "with Jev vs. without Jev" page. Reached from the header
// button in App.tsx via plain view state — this project has no router and
// deliberately isn't getting one for a single secondary page.
//
// Everything here is written to survive a half-populated response: the
// endpoint is new and experimental, and a missing field should degrade to
// an em dash rather than blanking or crashing the page.

interface CostComparisonPageProps {
  onBack: () => void;
}

function formatCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "—";
}

function formatUsd(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  // Six decimals: Jev-side figures are fractions of a cent, and rounding
  // them to the usual two would render every one of them as "$0.00".
  return `$${value.toFixed(6)}`;
}

// "assumed_agent_turn_input_tokens" -> "assumed agent turn input tokens".
// Deliberately generic: the backend may add or rename assumption keys and
// this page should keep showing its work without a frontend change.
function humanizeKey(key: string): string {
  return key.replace(/_/g, " ");
}

function formatAssumptionValue(value: number | string | null): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    return Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 6 }) : "—";
  }
  return String(value);
}

export function CostComparisonPage({ onBack }: CostComparisonPageProps) {
  const [data, setData] = useState<CostComparison | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getCostComparison()
      .then((result) => {
        if (!cancelled) setData(result);
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

  const assumptionEntries = data?.assumptions ? Object.entries(data.assumptions) : [];
  const noCallsYet = !!data && !data.jev_calls_total;

  return (
    <div className="cost-page">
      <div className="cost-page-header">
        <h2>Cost comparison: with Jev vs. without</h2>
        <button type="button" className="chat-new-button" onClick={onBack}>
          Back to console
        </button>
      </div>

      {loading && <p className="muted">Loading cost comparison…</p>}
      {error && <p className="error">{error}</p>}

      {!loading && !error && data && (
        <>
          {noCallsYet && (
            <p className="cost-empty-state">
              No Jev calls have been made yet, so there's nothing to compare. The typed-decision
              fast path ships turned off — once it's enabled and a message misses the regex
              parser, calls will start showing up here.
            </p>
          )}

          <section className="panel-section">
            <h3>Tally</h3>
            <table className="data-table">
              <tbody>
                <tr>
                  <th scope="row">Jev calls made</th>
                  <td>{formatCount(data.jev_calls_total)}</td>
                </tr>
                <tr>
                  <th scope="row">Fast-path hits (handled without Claude)</th>
                  <td>{formatCount(data.jev_fast_path_hits)}</td>
                </tr>
                <tr>
                  <th scope="row">Fall-throughs (went on to the agent)</th>
                  <td>{formatCount(data.jev_fallthroughs)}</td>
                </tr>
                <tr>
                  <th scope="row">Jev input tokens</th>
                  <td>{formatCount(data.jev_input_tokens)}</td>
                </tr>
                <tr>
                  <th scope="row">Actual Jev cost</th>
                  <td>{formatUsd(data.jev_cost_usd)}</td>
                </tr>
                <tr>
                  <th scope="row">Agent turns avoided</th>
                  <td>{formatCount(data.avoided_agent_turns)}</td>
                </tr>
                <tr>
                  <th scope="row">Estimated Sonnet cost avoided</th>
                  <td>{formatUsd(data.avoided_sonnet_cost_usd)}</td>
                </tr>
                <tr className="cost-net-row">
                  <th scope="row">Net savings</th>
                  <td>{formatUsd(data.net_savings_usd)}</td>
                </tr>
              </tbody>
            </table>
          </section>

          <p className="cost-caveat">
            <strong>The "without Jev" figure is an estimate, not a measurement.</strong> Nobody
            actually ran these turns through Claude to compare. It's built from token counts
            measured in this codebase — the agent's system prompt (≈440 tokens) plus its tool
            schemas (≈1,395 tokens), so ≈1,835 fixed input tokens on every agent turn — plus an
            assumed typical turn size on top. Real agent turns vary, so treat the savings number
            as an order-of-magnitude indication rather than an accounting figure.
          </p>

          {assumptionEntries.length > 0 && (
            <section className="panel-section">
              <h3>Assumptions used</h3>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Assumption</th>
                    <th>Value</th>
                  </tr>
                </thead>
                <tbody>
                  {assumptionEntries.map(([key, value]) => (
                    <tr key={key}>
                      <td>{humanizeKey(key)}</td>
                      <td>{formatAssumptionValue(value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </>
      )}
    </div>
  );
}

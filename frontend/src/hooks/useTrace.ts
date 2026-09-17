import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AgentEvent } from "../api/types";

export interface TraceState {
  events: AgentEvent[];
  loading: boolean;
  error: string | null;
  hasFetched: boolean;
}

/**
 * The one place `/api/sessions/{id}/trace` gets polled. Lifted out of
 * TracePanel so ChatPanel's in-chat progress indicator (a compact view of
 * the same events) can share this exact fetch/state instead of opening a
 * second, parallel polling path against the same endpoint — see
 * backend/app/api/routes/sessions.py's module docstring for why the
 * endpoint itself is polling-based rather than a stream.
 */
export function useTrace(sessionId: string | null, refreshToken: number): TraceState {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [hasFetched, setHasFetched] = useState(false);

  useEffect(() => {
    if (!sessionId) {
      setEvents([]);
      setHasFetched(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getTrace(sessionId)
      .then((data) => {
        if (!cancelled) {
          setEvents(data);
          setHasFetched(true);
        }
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
    // refreshToken intentionally triggers a re-fetch even though it isn't
    // read in the body — it changes once per completed chat turn.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, refreshToken]);

  return { events, loading, error, hasFetched };
}

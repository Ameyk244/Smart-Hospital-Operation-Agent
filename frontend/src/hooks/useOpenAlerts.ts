import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Alert } from "../api/types";

const POLL_INTERVAL_MS = 30_000;

// Polls only while the scheduler is on; a failed poll keeps the last list.
export function useOpenAlerts(enabled: boolean): Alert[] {
  const [alerts, setAlerts] = useState<Alert[]>([]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    function poll() {
      api
        .listAlerts("open")
        .then((data) => {
          if (!cancelled) setAlerts(data);
        })
        .catch(() => {});
    }
    poll();
    const id = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [enabled]);

  return enabled ? alerts : [];
}

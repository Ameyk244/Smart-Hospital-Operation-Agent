import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { StatusResponse } from "../api/types";

const POLL_INTERVAL_MS = 15_000;

export interface StatusState {
  status: StatusResponse | null;
  // The last fetch failed, i.e. the backend itself isn't answering. `status`
  // keeps the last known value, but it can no longer be trusted.
  unreachable: boolean;
}

export function useStatus(): StatusState {
  const [state, setState] = useState<StatusState>({ status: null, unreachable: false });

  useEffect(() => {
    let cancelled = false;
    // A slow earlier request must not overwrite the result of a newer one.
    let latest = 0;
    function poll() {
      const mine = ++latest;
      api
        .getStatus()
        .then((status) => {
          if (!cancelled && mine === latest) setState({ status, unreachable: false });
        })
        .catch(() => {
          if (!cancelled && mine === latest) setState((prev) => ({ ...prev, unreachable: true }));
        });
    }
    poll();
    const id = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return state;
}

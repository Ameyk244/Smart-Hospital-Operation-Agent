import type {
  AgentEvent,
  Alert,
  ScheduledTask,
  Appointment,
  Bed,
  CancelBedActionResponse,
  ConfirmBedActionResponse,
  PendingBedAction,
  ChatResponse,
  ConversationMessage,
  CancelRescheduleResponse,
  ConfirmRescheduleResponse,
  CostComparison,
  Department,
  Journey,
  Patient,
  PendingReschedule,
  QueueEntry,
  Scanner,
  StatusResponse,
  Ward,
} from "./types";

// Configurable via VITE_API_BASE_URL (see .env.example); defaults to the
// backend's dev port per docs/ARCHITECTURE.md.
const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL as string | undefined;
const API_BASE_URL: string = configuredApiBaseUrl
  ? (
      /^https?:\/\//i.test(configuredApiBaseUrl)
        ? configuredApiBaseUrl
        : `https://${configuredApiBaseUrl}`
    ).replace(/\/$/, "")
  : "http://localhost:8000";

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (body?.detail) detail = JSON.stringify(body.detail);
    } catch {
      // response wasn't JSON; fall back to statusText
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

function buildQuery(params: Record<string, string | number | undefined | null>): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      usp.set(key, String(value));
    }
  }
  const qs = usp.toString();
  return qs ? `?${qs}` : "";
}

export const api = {
  health(): Promise<{ status: string }> {
    return request("/api/health");
  },

  listDepartments(): Promise<Department[]> {
    return request("/api/operations/departments");
  },

  listScanners(filters: { type?: string; status?: string } = {}): Promise<Scanner[]> {
    return request(`/api/operations/scanners${buildQuery(filters)}`);
  },

  searchAppointments(
    filters: {
      status?: string;
      appointment_type?: string;
      patient_code?: string;
      department_code?: string;
      scanner_code?: string;
      limit?: number;
    } = {},
  ): Promise<Appointment[]> {
    return request(`/api/operations/appointments${buildQuery(filters)}`);
  },

  searchPatients(query: string): Promise<Patient[]> {
    return request(`/api/operations/patients${buildQuery({ query })}`);
  },

  listWards(): Promise<Ward[]> {
    return request("/api/operations/wards");
  },

  listBeds(filters: { ward_code?: string; status?: string } = {}): Promise<Bed[]> {
    return request(`/api/operations/beds${buildQuery(filters)}`);
  },

  listQueue(filters: { urgency?: string; status?: string } = {}): Promise<QueueEntry[]> {
    return request(`/api/operations/queue${buildQuery(filters)}`);
  },

  getJourney(patientCode: string): Promise<Journey> {
    return request(`/api/operations/journey${buildQuery({ patient_code: patientCode })}`);
  },

  listAlerts(status: "open" | "all" = "open"): Promise<Alert[]> {
    return request(`/api/operations/alerts${buildQuery({ status })}`);
  },

  listScheduledTasks(): Promise<ScheduledTask[]> {
    return request("/api/operations/scheduled-tasks");
  },

  sendChat(text: string, sessionId?: string): Promise<ChatResponse> {
    return request("/api/chat", {
      method: "POST",
      body: JSON.stringify({ text, session_id: sessionId }),
    });
  },

  getTrace(sessionId: string): Promise<AgentEvent[]> {
    return request(`/api/sessions/${encodeURIComponent(sessionId)}/trace`);
  },

  getMessages(sessionId: string): Promise<ConversationMessage[]> {
    return request(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
  },

  getPendingReschedules(sessionId: string): Promise<PendingReschedule[]> {
    return request(`/api/sessions/${encodeURIComponent(sessionId)}/pending-reschedules`);
  },

  confirmReschedule(sessionId: string, proposalId: number): Promise<ConfirmRescheduleResponse> {
    return request(`/api/sessions/${encodeURIComponent(sessionId)}/reschedule/${proposalId}/confirm`, {
      method: "POST",
    });
  },

  cancelReschedule(sessionId: string, proposalId: number): Promise<CancelRescheduleResponse> {
    return request(`/api/sessions/${encodeURIComponent(sessionId)}/reschedule/${proposalId}/cancel`, {
      method: "POST",
    });
  },

  getPendingBedActions(sessionId: string): Promise<PendingBedAction[]> {
    return request(`/api/sessions/${encodeURIComponent(sessionId)}/pending-bed-actions`);
  },

  confirmBedAction(sessionId: string, proposalId: number): Promise<ConfirmBedActionResponse> {
    return request(`/api/sessions/${encodeURIComponent(sessionId)}/bed-actions/${proposalId}/confirm`, {
      method: "POST",
    });
  },

  cancelBedAction(sessionId: string, proposalId: number): Promise<CancelBedActionResponse> {
    return request(`/api/sessions/${encodeURIComponent(sessionId)}/bed-actions/${proposalId}/cancel`, {
      method: "POST",
    });
  },

  // With-Jev vs. without-Jev cost tally. Omitting sessionId asks for the
  // all-sessions total; passing one scopes it to that session.
  getCostComparison(sessionId?: string): Promise<CostComparison> {
    return request(`/api/cost-comparison${buildQuery({ session_id: sessionId })}`);
  },

  getStatus(): Promise<StatusResponse> {
    return request("/api/status");
  },
};

export { ApiError, API_BASE_URL };

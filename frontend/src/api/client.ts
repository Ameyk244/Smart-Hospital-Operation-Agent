import type {
  AgentEvent,
  Appointment,
  ChatResponse,
  ConversationMessage,
  Department,
  Patient,
  Scanner,
} from "./types";

// Configurable via VITE_API_BASE_URL (see .env.example); defaults to the
// backend's dev port per docs/ARCHITECTURE.md.
const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";

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
      detail = body?.detail ? JSON.stringify(body.detail) : detail;
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
};

export { ApiError, API_BASE_URL };

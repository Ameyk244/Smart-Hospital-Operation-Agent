// TypeScript mirrors of the backend's Pydantic response shapes.
// Kept intentionally close to backend/app/schemas/*.py so the two stay easy
// to compare side by side. Field names match the JSON exactly.

export interface Department {
  code: string;
  name: string;
}

export interface Staff {
  code: string;
  name: string;
  role: string;
}

export type ScannerType = "MRI" | "CT" | "XRAY";
export type ScannerStatus = "AVAILABLE" | "IN_USE" | "MAINTENANCE";

export interface Scanner {
  code: string;
  name: string;
  type: ScannerType;
  status: ScannerStatus;
}

export interface Patient {
  code: string;
  mrn: string;
  name: string;
  date_of_birth: string;
}

export type AppointmentStatus = "SCHEDULED" | "COMPLETED" | "DELAYED" | "CANCELLED" | string;

export interface Appointment {
  code: string;
  patient: Patient;
  department: Department;
  scanner: Scanner | null;
  staff: Staff | null;
  appointment_type: ScannerType;
  scheduled_start: string;
  scheduled_end: string;
  status: AppointmentStatus;
  notes: string | null;
}

// --- /api/chat ---

export type HandledBy = "deterministic" | "agent" | "rejected";

export interface ChatRequest {
  text: string;
  session_id?: string;
}

export interface ChatResponse {
  session_id: string;
  handled_by: HandledBy;
  message: string;
  data: unknown | null;
  terminated_reason: string | null;
  // Every entity code (appointment/scanner/patient/department) this turn's
  // command or tool results touched — used to briefly highlight the
  // corresponding rows in the Operations panel.
  touched_entity_codes: string[];
}

// --- /api/sessions/{id}/messages ---

export type MessageRole = "USER" | "ASSISTANT";

export interface ConversationMessage {
  role: MessageRole;
  content: string;
  created_at: string;
}

// --- /api/sessions/{id}/trace ---

export type EventStatus = "SUCCESS" | "FAILURE" | "REJECTED" | string;

export interface AgentEvent {
  round_num: number;
  event_type: string;
  tool_name: string | null;
  arguments_json: Record<string, unknown> | null;
  status: EventStatus;
  latency_ms: number | null;
  error_category: string | null;
  created_at: string;
}

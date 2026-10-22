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

// --- /api/operations/wards, beds, queue, journey ---

export interface Ward {
  code: string;
  name: string;
  department: Department;
  bed_count: number;
  occupied_count: number;
}

export type BedStatus = "AVAILABLE" | "OCCUPIED" | "CLEANING";

export interface Bed {
  code: string;
  ward: { code: string; name: string };
  status: BedStatus;
  patient: { code: string; name: string; mrn: string } | null;
  occupied_since: string | null;
}

export type QueueUrgency = "CRITICAL" | "URGENT" | "STANDARD";
export type QueueStatus = "WAITING" | "ADMITTED" | "DISCHARGED";

export interface QueueEntry {
  code: string;
  patient: { code: string; name: string };
  urgency: QueueUrgency;
  status: QueueStatus;
  arrived_at: string;
  triaged_at: string | null;
  bed: { code: string; ward: { code: string; name: string } } | null;
  discharged_at: string | null;
  wait_minutes: number;
}

export type JourneyStageName = "ARRIVED" | "TRIAGED" | "BED_ASSIGNED" | "SCAN" | "DISCHARGED";

export interface JourneyStage {
  stage: JourneyStageName;
  reached: boolean;
  at: string | null;
  detail: string | null;
}

export interface Journey {
  patient: { code: string; name: string };
  stages: JourneyStage[];
  current_stage: string;
  appointments: Appointment[];
}

// --- /api/chat ---

// "jev" is the experimental typed-decision fast path: the regex parser
// missed, Jev mapped the message to a known deterministic command with
// enough confidence, and it executed through the normal CommandRunner
// with no Claude call at all.
export type HandledBy = "deterministic" | "agent" | "rejected" | "jev";

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
  // Appointment moves the agent proposed that wait for a human Confirm or
  // Cancel. Absent or empty when nothing is pending.
  pending_confirmations?: PendingReschedule[];
  pending_bed_actions?: PendingBedAction[];
}

// --- reschedule confirmation ---

export interface PendingReschedule {
  proposal_id: number;
  appointment_code: string;
  scanner_code: string;
  new_start: string | null;
}

export interface ConfirmRescheduleResponse {
  status: "confirmed";
  message: string;
  touched_entity_codes: string[];
}

export interface CancelRescheduleResponse {
  status: "cancelled";
  message: string;
}

// --- bed action confirmation ---

export interface PendingBedAction {
  proposal_id: number;
  action: "assign_bed" | "release_bed";
  bed_code: string;
  patient_code: string | null;
}

export interface ConfirmBedActionResponse {
  status: "confirmed";
  message: string;
  touched_entity_codes: string[];
}

export interface CancelBedActionResponse {
  status: "cancelled";
  message: string;
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

// Shape of a "jev_invoked" event's arguments_json. Not a separate event
// interface — AgentEvent already covers the row; this just names what the
// generic Record<string, unknown> bag holds for that one event type, and
// is only ever applied after a runtime-tolerant read (see TracePanel).
export interface JevDecisionArgs {
  choice: string | null;
  confidence: number | null;
  probabilities: Record<string, number> | null;
  threshold: number;
  input_tokens: number | null;
  output_tokens: number | null;
  model: string | null;
}

// --- /api/cost-comparison ---

// Every numeric field is typed nullable on purpose: the endpoint is new
// and experimental, and the page must render something sane rather than
// crash if the backend omits a field or sends null. `assumptions` is an
// open bag — the page iterates it instead of hardcoding key names.
export interface CostComparison {
  jev_calls_total: number | null;
  jev_fast_path_hits: number | null;
  jev_fallthroughs: number | null;
  jev_input_tokens: number | null;
  jev_cost_usd: number | null;
  avoided_agent_turns: number | null;
  avoided_sonnet_cost_usd: number | null;
  net_savings_usd: number | null;
  assumptions: Record<string, number | string | null> | null;
}

// --- /api/operations/alerts, scheduled-tasks ---

export type WatchCondition = "QUEUE_WAIT_OVER" | "FREE_BEDS_UNDER" | "DELAYED_APPOINTMENTS_OVER";

export interface Alert {
  id: number;
  task_code: string;
  severity: string;
  summary: string;
  entity_codes: string[];
  created_at: string;
  acknowledged_at: string | null;
}

export interface ScheduledTask {
  code: string;
  condition: WatchCondition;
  threshold: number;
  scope_code: string | null;
  period_seconds: number;
  active: boolean;
  last_run_at: string | null;
}

// --- /api/status ---

// on = working, off = not configured or switched off, down = configured but
// not answering.
export type Light = "on" | "off" | "down";

export interface StatusResponse {
  agent: { runtime: "in_process" | "harness"; status: Light };
  harness: { status: Light };
  jev: { status: Exclude<Light, "down"> };
  mode: 1 | 2 | 3;
  scheduler?: { status: "on" | "off" };
}

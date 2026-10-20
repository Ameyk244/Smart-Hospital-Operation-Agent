# Expansion: wards and beds, emergency queue, patient journey

Three modules added next to the existing operations view, plus a sidebar and a
visual refresh. Nothing existing changes behaviour. The agent stays one global
agent: no tool, gate or prompt depends on the page the user is on.

## 1. Scope and rules carried over

- Routing is unchanged: parser, Jev, domain gate, eligibility gate, agent.
  New commands extend the parser grammar, the Jev read-only list, the gate
  vocabulary and the tool registry; nothing is duplicated outside `CommandRunner`.
- Jev may only name the new **read** commands. It never runs a write.
- Grounding-by-rejection applies to both new writes. Reads expose codes; writes
  require them.
- Both new writes are `audited=True` commands (one audit record each, in the
  same transaction). Their arguments go on the redaction allowlist.
- The UI stays read-only; changes are made through chat. No page references
  Grafana or telemetry.
- Both writes can require confirmation (section 7a), opt-in through
  `REQUIRE_BED_CONFIRMATION` (default off, like `REQUIRE_RESCHEDULE_CONFIRMATION`).
  The reschedule confirmation flow keeps its behaviour.
- This ends the invariant "`reschedule_appointment` is the only tool that
  mutates hospital data". `assign_bed` and `release_bed` also do. Update
  CLAUDE.md and `rulebook.md` when the code lands.

## 2. Data (new tables; migration needs approval in the turn it is run)

| Table | Columns |
|---|---|
| `wards` | `id`, `code` (WRD-n, unique), `name`, `department_id` -> departments |
| `beds` | `id`, `code` (BED-n, unique), `ward_id` -> wards, `status` (`AVAILABLE`/`OCCUPIED`/`CLEANING`), `patient_id` -> patients (null unless OCCUPIED), `occupied_since` (null unless OCCUPIED) |
| `queue_entries` | `id`, `code` (ER-n, unique), `patient_id` -> patients, `urgency` (`CRITICAL`/`URGENT`/`STANDARD`), `status` (`WAITING`/`ADMITTED`/`DISCHARGED`), `arrived_at`, `triaged_at` (null), `bed_id` -> beds (null), `discharged_at` (null) |

No free-text clinical columns (no complaint, no notes): nothing new that needs
redacting beyond ids and enums.

The patient journey has **no table**. It is derived on read from the patient's
latest queue entry, its bed and the patient's appointments.

## 3. Commands (all in `CommandRunner`)

| Command | Kind | Args |
|---|---|---|
| `list_wards` | read | none |
| `list_beds` | read | `status?`, `ward_code?` |
| `list_emergency_queue` | read | `urgency?` |
| `patient_journey` | read | `query` (name) |
| `assign_bed` | write, audited | `patient_code`, `bed_code` |
| `release_bed` | write, audited | `bed_code` |

Business rules (enforced in the handler, not by callers):

- `assign_bed`: bed must be `AVAILABLE`; the patient must have a `WAITING` queue
  entry and no bed. Effect: bed `OCCUPIED` (+ `patient_id`, `occupied_since`),
  entry `ADMITTED` with `bed_id`.
- `release_bed`: bed must be `OCCUPIED`. Effect: bed `CLEANING` (patient cleared),
  the patient's `ADMITTED` entry becomes `DISCHARGED` with `discharged_at`.
- A rule violation raises `CommandError` (categories `bed_unavailable`,
  `no_waiting_entry`, `already_bedded`, `bed_not_occupied`); unknown codes raise
  `ValueError`, exactly as `reassign_scanner` does.
- Audit `before`/`after` carry only codes and statuses, never names.

## 4. Parser grammar (exact, `$`-anchored, same normalisation)

```
list wards
list beds [available|occupied|cleaning]...
(show|list) emergency queue
show (patient )?journey <name>      # placed before "show patient <name>"
```

## 5. Agent tools

| Tool | Kind | Grounding |
|---|---|---|
| `search_beds` | read | exposes `bed` codes |
| `search_queue` | read | exposes `queue_entry` and `patient` codes |
| `get_patient_journey` | read | exposes `patient` code |
| `assign_bed` | write | requires grounded `patient` and `bed` |
| `release_bed` | write | requires grounded `bed` |

`execute_command` keeps working for the new read commands automatically because
it dispatches by command name.

## 6. HTTP contract (read-only, under `/api/operations`)

```
GET /wards                      -> Ward[]
GET /beds?ward_code=&status=    -> Bed[]
GET /queue?urgency=&status=     -> QueueEntry[]
GET /journey?patient_code=      -> Journey
```

```ts
type Ward = { code: string; name: string; department: { code: string; name: string };
              bed_count: number; occupied_count: number };
type Bed = { code: string; ward: { code: string; name: string };
             status: "AVAILABLE" | "OCCUPIED" | "CLEANING";
             patient: { code: string; name: string; mrn: string } | null;
             occupied_since: string | null };
type QueueEntry = { code: string; patient: { code: string; name: string };
                    urgency: "CRITICAL" | "URGENT" | "STANDARD";
                    status: "WAITING" | "ADMITTED" | "DISCHARGED";
                    arrived_at: string; triaged_at: string | null;
                    bed: { code: string; ward: { code: string; name: string } } | null;
                    discharged_at: string | null;
                    wait_minutes: number };   // minutes waited so far; 0 once admitted
type JourneyStage = { stage: "ARRIVED" | "TRIAGED" | "BED_ASSIGNED" | "SCAN" | "DISCHARGED";
                      reached: boolean;   // the UI shows done/not done from this, never from `at`
                      at: string | null; detail: string | null };
type Journey = { patient: { code: string; name: string };
                 stages: JourneyStage[]; current_stage: string;
                 appointments: Appointment[] };   // existing Appointment type
```

Every object carries `code`, so `extract_entity_codes` and row highlighting work
with no change.

## 7. Frontend

- A left **sidebar** replaces the header button as navigation: Operations,
  Wards, Emergency queue, Patient journey, Cost comparison. Chat and trace stay
  in the right column on every page. Still no router: the existing `view`
  switch in `App.tsx` is extended.
- Page components (fixed names so they can be built in parallel):
  `WardsPage`, `EmergencyQueuePage`, `JourneyPage`, each taking
  `{ touchedEntityCodes: string[] }`, in `frontend/src/components/`.
- Styling: shared tokens and shell in `index.css` / `App.css`; the new pages use
  their own `frontend/src/components/pages.css`.
- Visual refresh covers the shell, header, panels, tables, status badges and
  spacing. Behaviour, props, text content that tests assert on, and the chat and
  trace panels' structure do not change.

## 7a. Bed confirmation (opt-in)

`REQUIRE_BED_CONFIRMATION=true` makes the agent's `assign_bed` / `release_bed`
only record a proposal; the bed changes when the user confirms in the app. Off:
both act immediately, as before. The parser has no bed write, so only the agent
path is gated. Same mechanism as reschedule, no schema change (it reuses
`agent_events`):

- Events `bed_action_proposed`, `bed_action_confirmed`, `bed_action_cancelled`;
  the proposal's `tool_name` is `assign_bed` or `release_bed` and its
  `arguments_json` is the command's arguments (already on the redaction list).
- The tool returns `{status: "pending_confirmation", proposal_id, message}` and
  tells the model the bed was NOT changed.
- `ChatResponse.pending_bed_actions: PendingBedAction[]`, filled only while the
  flag is on:
  `type PendingBedAction = { proposal_id: number; action: "assign_bed" | "release_bed";
  bed_code: string; patient_code: string | null }`.
- Routes (session-scoped, like reschedule):
  `GET  /api/sessions/{sid}/pending-bed-actions` -> `PendingBedAction[]`
  `POST /api/sessions/{sid}/bed-actions/{pid}/confirm` -> `{status, message, touched_entity_codes}`
  `POST /api/sessions/{sid}/bed-actions/{pid}/cancel`  -> `{status, message}`
  Confirm runs the same command through `CommandRunner` as
  `user_confirmed:{sid}`; 404 unknown proposal, 409 already resolved or the
  command failed. Both outcomes are added to the chat history and the checkpoint.
- UI: a confirmation card in the chat for each pending action (Confirm / Cancel).

## 8. Not built (on purpose)

- Writes from the UI; adding patients to the queue; triage edits.
- A journey events table; a capacity forecast.
- Jev measurement for the new read commands against a labelled set (needs a live
  run the owner must approve).

## 9. Build order and ownership

1. Backend: models, repositories, commands, routes, tools, parser, gate, Jev,
   seed, migration file (written, **not run**), tests. One agent, because these
   share files.
2. Frontend, in parallel with each other: pages (new files plus `api/`) and the
   shell/refresh (`App.tsx`, `App.css`, `index.css`).
3. Review every diff against its scope; run the suites one at a time.
4. Owner approves: Alembic upgrade and seed on the dev database.

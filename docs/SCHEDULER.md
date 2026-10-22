# Scheduled watches and alerts

The app only reacts today: nothing runs unless someone sends a message. This adds
repeatable, **read-only** work that the backend runs by itself and shows in the UI.
Status: built on branch `harness-features`, off by default. Two new tables (migration
`b7d2f4a96c18`, additive).

## 1. Scope and rules carried over

- Backend-owned. dsh's own `schedule` plugin stores prompts and needs a restorable
  session, which the gateway forbids (no stored chat text, fresh agent per turn).
- Read-only by construction. A task names one existing **read** command from the
  parser grammar (`list wards`, `show emergency queue`, ...) and runs it through
  `CommandRunner`, the single execution chokepoint. No write tool is reachable, and no
  LLM is involved in a watch.
- Tasks are created through chat (a parser command, so the cheap path stays
  deterministic, or the agent's scheduler tools), never from the UI; the UI only shows alerts, as it only reads elsewhere.
- No patient names or free text are stored: a task holds a command name, its
  arguments (ids and enums only), a threshold and a period. An alert holds the task id,
  a short enum-based message and entity codes. Both go on the redaction allowlist.
- Off by default (`SCHEDULER_ENABLED=false`). With it off nothing starts, the
  new commands answer "The scheduler is off" and the HTTP routes return `[]`.

## 2. Data (migration needs approval in the turn it is run)

| Table | Columns |
|---|---|
| `scheduled_tasks` | `id`, `code` (TSK-n, unique), `kind` (`WATCH`), `condition` (enum), `threshold` (int), `scope_code` (nullable), `period_seconds`, `active`, `last_run_at`, `next_run_at`, `created_at`. The command each condition reads comes from the condition, not a column |
| `alerts` | `id`, `task_id` -> scheduled_tasks, `dedupe_key`, `severity` (`INFO`/`WARNING`/`CRITICAL`), `summary` (templated from enums and numbers), `entity_codes` (JSON), `created_at`, `acknowledged_at` |

## 3. Runner

One asyncio task started in the FastAPI lifespan next to the checkpointer. Each tick
selects due active tasks with `FOR UPDATE SKIP LOCKED`, runs the command, evaluates the
condition in plain code, writes an alert when it holds (and none is open for the same task
and entity), and sets `next_run_at`. A failure of one task is recorded as an event and
never stops the loop. The telemetry path stays optional: the runner works with the
collector down.

## 4. Conditions, commands and API

Three watch conditions in the first version, each a pure function of one existing read
command's result:

| Condition | Command it runs | Fires when |
|---|---|---|
| `QUEUE_WAIT_OVER` N minutes | `list_emergency_queue` | a WAITING entry has `wait_minutes` > N (alert per entry code) |
| `FREE_BEDS_UNDER` N | `list_beds` (status AVAILABLE) | fewer than N available beds (alert per hospital, or per ward when a ward code is given) |
| `DELAYED_APPOINTMENTS_OVER` N | `search`/`list` of DELAYED appointments | more than N delayed appointments |

Default period 5 minutes (`SCHEDULER_TICK_SECONDS`, tick 30 s). Global to the hospital.

Parser (exact, `$`-anchored, same normalisation as the rest of the grammar):

- `watch queue wait over 30 minutes`
- `watch free beds under 2` / `watch free beds under 2 in WRD-1`
- `watch delayed appointments over 3`
- `list watches`, `stop watch TSK-2`
- `list alerts`, `acknowledge alert 5`, `acknowledge all alerts`

HTTP (read-only, `/api/operations`):

- `GET /alerts?status=open|all` -> `[{id, task_code, severity, summary, entity_codes, created_at, acknowledged_at}]`
- `GET /scheduled-tasks` -> `[{code, condition, threshold, scope_code, period_seconds, active, last_run_at}]`
- both return `[]` when the scheduler is off; `GET /api/status` adds `"scheduler": {"status": "on"|"off"}`.

UI: a bell with an open-alert count in the header, and an Alerts page in the sidebar listing
open alerts (severity, summary, codes, time) and the active watches. Read-only: acknowledging
and creating watches go through chat.

## 5. Not built (on purpose)

- Scheduled writes of any kind. Free-form cron. Narrated reports through the model
  (a later opt-in step that would use the harness with read tools only).
- Push, email or SMS delivery.

## 6. Decisions and known limits

- Global to the hospital, not per session. Period is a constant 5 minutes per watch;
  `SCHEDULER_TICK_SECONDS` only paces the loop.
- A threshold must be at least 1, an identical active watch is refused, and at most 20
  watches can be active. Stopping a watch acknowledges its open alerts.
- Alerts de-duplicate against **open** alerts only: acknowledging while the problem persists
  raises a fresh alert on the next check (a reminder, not silence). Silence the problem by
  stopping the watch.
- An open alert keeps the summary and codes it was raised with; it is not updated as the
  count changes.
- The tick holds row locks while it runs the checks, so `stop watch` can wait behind a
  tick. Writes from chat are audited with actor `unattributed`, like every other
  command that runs without an agent session.
- The agent has five dedicated tools (`list_watches`, `list_alerts`, `create_watch`,
  `stop_watch`, `acknowledge_alert`), offered and callable only when the scheduler is on;
  with it off the registry is the original 14. The writes touch only the scheduler tables,
  count against `MAX_WRITE_CALLS_PER_TURN`, and `stop_watch`/`acknowledge_alert` need a
  code the session has seen (grounding types `watch` and `alert`). `execute_command` still
  refuses audited commands.
- Not done: alert expiry; the real lifespan start was checked live, not in a test.

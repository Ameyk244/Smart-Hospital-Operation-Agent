# Scheduled watches and reports (design, not built)

The app only reacts today: nothing runs unless someone sends a message. This adds
repeatable, **read-only** work that the backend runs by itself and shows in the UI.
Status: design for review. It needs two new tables, so the Alembic migration needs
explicit approval in the turn it is run. Nothing here is implemented.

## 1. Scope and rules carried over

- Backend-owned. dsh's own `schedule` plugin stores prompts and needs a restorable
  session, which the gateway forbids (no stored chat text, fresh agent per turn).
- Read-only by construction. A task names one existing **read** command from the
  parser grammar (`list wards`, `show emergency queue`, ...) and runs it through
  `CommandRunner`, the single execution chokepoint. No write tool is reachable, and no
  LLM is involved in a watch.
- Tasks are created through chat (a parser command, so the cheap path stays
  deterministic), never from the UI; the UI only shows alerts, as it only reads elsewhere.
- No patient names or free text are stored: a task holds a command name, its
  arguments (ids and enums only), a threshold and a period. An alert holds the task id,
  a short enum-based message and entity codes. Both go on the redaction allowlist.
- Off by default (`SCHEDULER_ENABLED=false`). With it off nothing starts and the
  parser does not recognise the new commands.

## 2. Data (migration needs approval in the turn it is run)

| Table | Columns |
|---|---|
| `scheduled_tasks` | `id`, `code` (TSK-n, unique), `session_id` -> agent_sessions, `kind` (`WATCH`/`REPORT`), `command` (a read command name), `arguments_json`, `condition` (enum, e.g. `QUEUE_WAIT_OVER`), `threshold` (int, nullable), `period_seconds`, `active`, `last_run_at`, `next_run_at` |
| `alerts` | `id`, `task_id` -> scheduled_tasks, `created_at`, `severity` (enum), `summary` (templated from enums and numbers), `entity_codes` (JSON), `acknowledged_at` |

## 3. Runner

One asyncio task started in the FastAPI lifespan next to the checkpointer. Each tick
selects due active tasks with `FOR UPDATE SKIP LOCKED`, runs the command, evaluates the
condition in plain code, writes an alert when it holds (and none is open for the same task
and entity), and sets `next_run_at`. A failure of one task is recorded as an event and
never stops the loop. The telemetry path stays optional: the runner works with the
collector down.

## 4. Commands and API

- Parser: `watch emergency queue wait over 30 minutes`, `list watches`, `stop watch TSK-2`,
  `acknowledge alert 5`. Exact, `$`-anchored, same normalisation as the rest of the grammar.
- HTTP (read-only, under `/api/operations`): `GET /alerts`, `GET /scheduled-tasks`.
- UI: an alert badge and a list; acknowledging goes through chat.

## 5. Not built (on purpose)

- Scheduled writes of any kind. Free-form cron. Narrated reports through the model
  (a later opt-in step that would use the harness with read tools only).
- Push, email or SMS delivery.

## 6. Open decisions

- Allowed conditions in the first version (proposal: ER wait over N minutes, free beds
  under N in a ward).
- Whether alerts expire. Whether tasks are per session or global to the hospital
  (proposal: global, since an alert is for staff, not a conversation).

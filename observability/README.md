# Observability

The master reference for observability in the Smart Hospital Operations Agent:
what existed before this work, what each phase added, how to run the stack,
how to read the dashboard, and why each decision went the way it did.
`CONCEPT_MAP.md` (next to this file) maps the 10-module observability
curriculum onto the code; this file is the operational guide.

Everything for this work lives in this folder (docs, dashboard JSON, compose
file), not under `docs/`. Application code lives where application code
always lives: `backend/app/observability/`.

**Not part of the hospital UI.** Grafana is a separate tool, opened directly
at <http://localhost:3000>. No page, link, or button in `frontend/` refers to
it or to any telemetry endpoint, and none should be added in any future
deployment.

**No paid service, no new secret.** OpenTelemetry, the Collector, Prometheus,
Loki, Tempo, and Grafana all run locally in one self-hosted container.

---

## 1. Starting point (verified before Phase 1)

The audit this work is based on was re-verified against the code on
2026-10-01, at commit `56eda8d`, before anything was built. Since the audit,
nothing in `backend/app/observability/` had changed. Its last change was
`df4ce73`.

| Piece | Where | State |
|---|---|---|
| Structured JSON logs: structlog, `merge_contextvars`, log level, ISO timestamp, stack/exception info | `backend/app/observability/logging_config.py` | Confirmed unchanged |
| Action-level agent trace: one `agent_events` DB row plus one structlog line per decision point | `backend/app/observability/tracing.py` (`record_event`) | Confirmed unchanged |
| No chain-of-thought captured | `tracing.py` docstring, ARCHITECTURE §11 | Confirmed: `record_event` has no reasoning field |
| Trace panel API `GET /api/sessions/{id}/trace` | `backend/app/api/routes/sessions.py` | Confirmed |
| Cost endpoint `GET /api/cost-comparison` | `backend/app/api/routes/cost.py` | Confirmed |

Event types emitted. Re-checked against every `record_event` call site:

| Event type | Emitted from |
|---|---|
| `agent_invoked`, `llm_response`, `llm_timeout`, `max_rounds_exceeded` | `app/agent/graph.py` agent_node |
| `tool_requested` (rejected: `unknown_tool` / `invalid_argument`), `tool_executed` (success / `timeout` / `grounding_rejected` / tool error category) | `app/agent/graph.py` tool_node |
| `jev_invoked` | `app/api/routes/chat.py` |
| `voice_connection_dropped` | `app/api/routes/voice.py` |

**Corrections to the earlier audit**, found during re-verification:

1. The audit named the voice event only "a voice-dropped event". Its real
   name is `voice_connection_dropped`.
2. The structlog line that `record_event` writes does **not** include
   `arguments`. The PHI exposure is only in the `agent_events.arguments_json`
   DB column, which the trace panel then shows verbatim.
3. Leak vectors the audit didn't list:
   - `execute_command.command_text` is free text (`"show patient John Smith"`).
   - `remember_preference.key` / `value` are free text.
   - The `invalid_argument` branch stores the model's raw args, with whatever
     keys and values it invented.
4. A leak outside the agent trace: uvicorn's access log records the full
   request path including the query string. That means
   `GET /api/operations/patients?query=<patient name>` writes a patient name
   to stdout on every search.
5. `max_tool_calls_exceeded` and `too_many_invalid_calls` are terminations
   carried in agent state, not `record_event` rows. The trace panel never
   shows them. This is noted for Phase 3 metrics; the trace is not changed.

## 2. Build plan and log

Each phase is one commit on branch `observability`. This section is updated
as each phase lands.

| Phase | Scope | Status |
|---|---|---|
| 1 | PHI/secret redaction on existing trace + logs | Done |
| 2 | Request-ID middleware; `request_id`/`session_id` bound into structlog | Planned |
| 3 | OTel metrics: routing counter, agent/tool/LLM, voice STT, both DB pools | Planned |
| 4 | OTel traces: auto-instrumentation + manual spans along the request path | Planned |
| 5 | `grafana/otel-lgtm` stack + checked-in dashboard, verified with real local traffic | Planned |
| 6 | Audit record for `reschedule_appointment` (needs a migration, approval-gated) | Planned |
| 7 | 2–3 SLIs/SLOs as dashboard panels | Planned |

### Phase 1: PHI and secret redaction

This came first so that later instrumentation is built on a non-leaky
base. Everything is in `backend/app/observability/redaction.py`.

| Leak | Fix |
|---|---|
| Free-text tool args in `agent_events.arguments_json` (`command_text`, preference values, raw args of rejected calls) | `record_event` runs a **fail-closed allowlist** over `arguments`: each allowed key has a required value shape (entity code, enum, ISO datetime, number, identifier). Anything else becomes `[REDACTED]`. Keys are kept so the trace still shows what was passed; non-identifier keys are dropped and counted. |
| A model-invented `tool_name` on `unknown_tool` | Kept only if it's a plain identifier |
| `/api/operations/patients?query=<name>` in uvicorn's access log | A `logging.Filter` on `uvicorn.access` replaces the query string |
| Bound SQL parameters inside SQLAlchemy error text, and so inside logged tracebacks | `create_async_engine(..., hide_parameters=True)` |
| API keys or the DB password in any log line | A structlog processor, run after exception formatting, scrubs the configured secret values from every field. It also redacts keys like `user_text`, `query`, `*_api_key`. |

Entity codes are **kept** on purpose. They are pseudonymous internal keys
the session has already been shown, and a trace without them can't be
followed. Names, MRNs and free text are what gets removed.

**How to see it:** run a `show patient <name>` turn through the agent. The
trace panel shows `command_text: "[REDACTED]"`.

**Tests:**
- `tests/unit/test_redaction.py` (18 tests).
- `tests/integration/test_trace_redaction.py`: a scripted agent turn
  carries a real seeded patient's name through a successful lookup, a
  wrong-field search, and an `invalid_argument` rejection. It asserts that
  neither the full name nor the surname appears in the stored events or on
  stdout. This test was confirmed to **fail** against the pre-Phase-1
  `tracing.py`; there, the stored `command_text` contained the patient's
  name.

## 3. Decisions

- **Stack location: `observability/docker-compose.yml`, separate from the
  root compose file.** The app must run with telemetry absent, so the stack
  is opt-in rather than a dependency of `postgres`. Phase 5 finalizes this.
- **`agent_events` stays as it is.** It is a product feature (the UI trace
  panel). OTel adds operator-facing telemetry alongside it and replaces
  nothing.
- **Metric labels.** Labels are low-cardinality only. Per-request identifiers
  go on spans and log lines.

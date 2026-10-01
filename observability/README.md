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
| 2 | Request-ID middleware; `request_id`/`session_id` bound into structlog | Done |
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

### Phase 2: Correlation

Before this, nothing tied log lines together: two concurrent chats
interleaved on stdout, and an `agent.trace` line couldn't be matched to the
HTTP call that caused it. The middleware is in
`backend/app/observability/request_context.py`, added in `app/main.py` as
the outermost user middleware.

| Piece | What it does |
|---|---|
| `RequestContextMiddleware` (pure ASGI) | One `request_id` per HTTP request and per WebSocket connection, bound into structlog contextvars, so every line logged downstream carries it with no call site passing it. Reset with the bind tokens afterwards, so nothing bleeds into the next request. |
| `X-Request-ID` | A client-supplied id is kept only if it matches `^[A-Za-z0-9._-]{1,64}$`. Anything else (spaces, quotes, newlines, too long) is replaced with a uuid4 hex, so a client can't put free text into every log line. The id is echoed on the HTTP response and on the WebSocket accept. |
| `session_id` binding | `handle_chat_message` binds it for the turn (text and voice both go through it). The voice handler also binds it for the connection's lifetime, so STT lines carry it. Both use `bound_contextvars`, which restores the previous value on exit. |
| `request_completed` log line | One per HTTP request: `method`, `route` (the template, e.g. `/api/sessions/{session_id}/trace`, or `unmatched`), `status_code`, `duration_ms`. Logs alone now give RED per endpoint. Never the raw path, query string, or body. An unhandled exception is logged as 500. WebSockets get one `websocket_closed` line with route and duration. |

Why pure ASGI and not `BaseHTTPMiddleware`: the latter runs the endpoint
in a separate task, so contextvars bound inside don't come back out, and
it buffers streaming responses. A plain ASGI wrapper runs in the request's
own task.

`request_completed` carries `request_id` but not `session_id`. The session
binding is scoped to the turn inside the handler and is gone by the time
the middleware logs. Join on `request_id` to get from the request line to
the session's lines.

Known gap: when an exception escapes to Starlette's `ServerErrorMiddleware`,
the 500 it sends is produced outside this middleware, so that one response
has no `X-Request-ID` header. Its `request_completed` line still has the
id.

**How to see it:** `curl -i -X POST localhost:8000/api/chat -H
"Content-Type: application/json" -d '{"text":"list departments"}'`. The
response has an `X-Request-ID` header, and every JSON line the backend
printed for it (including `request_completed` with `"route": "/api/chat"`)
has the same `request_id`.

**Tests:**
- `tests/e2e/test_request_correlation.py` (9 tests, real app): all lines of
  a chat request share the echoed id; a safe incoming id is kept; unsafe
  ones are replaced and never printed; no leak of `request_id` or
  `session_id` between two sequential requests; a line logged inside the
  routing carries the `session_id`; `request_completed` logs route
  templates, never the patient-search query or path parameters.
- `tests/unit/test_request_context.py` (12 tests, toy app): id validation,
  an endpoint that raises is logged as 500, one id across a WebSocket
  connection plus the accept header, prior bindings restored rather than
  cleared.
- `tests/integration/test_voice_websocket.py::test_voice_connection_log_lines_carry_request_and_session_id`:
  the real voice handler's lines carry the connection's `request_id` and
  `session_id`, and the accept carries the header.
- Log lines are captured by the `captured_logs` fixture in
  `tests/conftest.py`. It patches the `print` that structlog's
  `PrintLogger` calls, so tests assert on the redacted JSON that really
  reaches stdout.

## 3. Decisions

- **Stack location: `observability/docker-compose.yml`, separate from the
  root compose file.** The app must run with telemetry absent, so the stack
  is opt-in rather than a dependency of `postgres`. Phase 5 finalizes this.
- **`agent_events` stays as it is.** It is a product feature (the UI trace
  panel). OTel adds operator-facing telemetry alongside it and replaces
  nothing.
- **Metric labels.** Labels are low-cardinality only. Per-request identifiers
  go on spans and log lines.

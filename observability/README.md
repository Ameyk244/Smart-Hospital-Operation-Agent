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
at <http://localhost:3001>. No page, link, or button in `frontend/` refers to
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
| 3 | OTel metrics: routing counter, agent/tool/LLM, Jev, voice STT + CPU, HTTP RED, SQLAlchemy pool gauge + checkpointer operation timing | Done |
| 4 | OTel traces: auto-instrumentation + manual spans along the request path; trace_id on logs; OTLP log export | Done |
| 5 | `grafana/otel-lgtm` stack + checked-in dashboard, verified with real local traffic | Done |
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

### Phase 3: Metrics

OpenTelemetry metrics, recorded through the OTel **API** and exported by
the **SDK** only when `OTEL_ENABLED=true`.

| Piece | Where |
|---|---|
| Every instrument, plus a one-line `record_*` helper per call site | `backend/app/observability/metrics.py` |
| SDK setup: `Resource` (`service.name=hospital-ops-backend`, `service.version`, `deployment.environment.name`), `MeterProvider`, periodic OTLP/HTTP export to `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/metrics` | `backend/app/observability/telemetry.py`, called from `app/main.py`'s lifespan |
| Settings: `otel_enabled` (default **false**), `otel_exporter_otlp_endpoint` (default `http://localhost:4318`), `otel_metric_export_interval_ms` (default 15000) | `backend/app/config.py`, `backend/.env.example` |
| Dependencies: `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, all `>=1.45,<1.46` (installed: 1.45.0) | `backend/requirements.txt` |

**API vs SDK.** With telemetry off (the default), the instruments come from
the API's no-op provider: every `add`/`record` is an empty call and nothing
leaves the process. A fresh clone and the test suite never try to reach a
collector. With it on, the SDK's periodic reader exports from a background
thread. If the collector is down, exports fail there and are logged; they
never raise into a request. Setup and shutdown are also wrapped so they
never stop the app. Known cost: with the collector down, shutdown waits up
to the 5 s export timeout.

**Metrics.** Names are OTel names. Prometheus, behind the collector,
rewrites them: dots become underscores, the unit is appended, and counters
get `_total`. So `chat.requests` appears as `chat_requests_total`, and
`llm.call.duration` (unit `s`) appears as `llm_call_duration_seconds_*`.
Verified against the running stack in Phase 5, with one addition: the
unit-`1` gauge `checkpointer.connection.up` appears as
`checkpointer_connection_up_ratio`.

| Metric | Type | Attributes | Question it answers |
|---|---|---|---|
| `chat.requests` | Counter | `route` (parser / jev / domain_rejected / ineligible / agent / error), `channel` (text / voice) | Parser hit rate, Jev hit rate, LLM fallback rate (share of `agent`), gate rejection rate, voice vs typed share |
| `chat.request.duration` | Histogram (s) | `route`, `channel` | How long each route takes to answer: parser p95 vs agent p95 |
| `http.server.request.duration` | Histogram (s) | `http.request.method`, `http.route` (template, or `unmatched`), `http.response.status_code` | RED per endpoint: rate = count, errors = 5xx share, duration = percentiles |
| `voice.connections.active` | UpDownCounter | none | How many voice WebSockets are open now |
| `voice.utterances` | Counter | `outcome` (routed / empty_transcript / stt_failed / internal_error) | How many utterances reach routing vs fail in STT/VAD |
| `voice.connections.dropped` | Counter | none | Connections lost mid-utterance (audio thrown away) |
| `stt.duration` | Histogram (s) | `status` (ok / error) | Local Whisper latency per utterance |
| `stt.audio.duration` | Histogram (s) | `status` | How long utterances are (samples / 16000) |
| `stt.real_time_factor` | Histogram (1) | `status` | STT seconds per audio second. Above 1 means slower than real time: the CPU is saturated. The first utterance after startup also includes loading the Whisper model, so expect one outlier |
| `process.cpu.time` | Observable counter (s) | none | `rate()` = CPU cores this process uses. USE utilization for the CPU-bound STT. From `time.process_time()`, so no `psutil` dependency |
| `jev.consultations` | Counter | `outcome` (matched / declined / failed) | How often Jev saves an agent turn, and how often it fails |
| `jev.duration` | Histogram (s) | `outcome` | Jev latency. Only recorded when Jev was actually called, not for the early returns (missing key/SDK, unsupported filter) |
| `jev.tokens` | Counter | `token_type` (input / output) | Jev token spend |
| `agent.rounds` | Histogram | `termination` | Model rounds per agent turn |
| `agent.terminations` | Counter | `termination` (completed / max_rounds_exceeded / llm_timeout / max_tool_calls_exceeded / too_many_invalid_calls) | How agent turns end. Includes the two terminations that never write an `agent_events` row |
| `llm.call.duration` | Histogram (s) | `status` (ok / timeout / error), `model` (the one configured model name) | LLM latency and timeout rate |
| `llm.tokens` | Counter | `token_type`, `model` | LLM token spend, from the response's `usage_metadata` |
| `agent.tool.calls` | Counter | `tool_name` (registered name, or `unknown` for a model-invented one), `status` (success / failure / rejected), `error_category` (`none`, `grounding_rejected`, `invalid_argument`, `unknown_tool`, `timeout`, `budget_exhausted`, or a tool error category) | Tool failure rate, grounding-rejection rate, invalid-call rate per tool |
| `agent.tool.duration` | Histogram (s) | `tool_name`, `status` | Tool latency (executed calls only, not validation rejections) |
| `db.pool.connections` | Observable gauge | `pool=sqlalchemy`, `state` (size / checked_out / overflow) | SQLAlchemy `QueuePool` saturation: `checked_out` near `size` + max overflow means requests queue for a connection |
| `checkpointer.operation.duration` | Histogram (s) | `operation` (get_tuple / put / put_writes), `status` | LangGraph checkpointer latency, including the wait for its lock |
| `checkpointer.connection.up` | Observable gauge | none | 1 while the checkpointer's connection is open |

**The DB is one pool plus one connection, not two pools.** The app's
SQLAlchemy engine has a real `QueuePool`, which gets size, checked-out and
overflow gauges. The LangGraph checkpointer is **one long-lived psycopg
connection** (`AsyncPostgresSaver.from_conn_string`), and every
checkpoint operation takes the saver's own `asyncio.Lock` first. It has no
pool size or checked-out count. Its saturation shows up as a rising
`checkpointer.operation.duration`: concurrent agent turns queue on that
lock. That is what we measure, through `InstrumentedAsyncPostgresSaver` in
`app/agent/checkpointer.py`. It is a subclass that only adds timing. We
also report whether its connection is still open. It was deliberately not
converted to a pool.

**Cardinality.** Attributes come only from fixed sets. `session_id`,
`request_id`, entity codes, patient names and user text never appear as
attributes. A model-invented tool name is recorded as `unknown`, an
unusual HTTP method as `_OTHER`, and an unmatched path as `unmatched`. The
model attribute is the single configured model name.

**How to see it:** with the Phase 5 stack running and `OTEL_ENABLED=true`,
open Grafana at <http://localhost:3001> → Explore → Prometheus →
`sum by (route) (rate(chat_requests_total[5m]))`. Without the stack, run
`pytest tests/integration/test_metrics.py -v`.

**Tests** (all use an in-memory reader, with no collector):
- `tests/integration/test_metrics.py` (13 tests):
  - each `chat.requests` route through the real HTTP app (parser,
    domain_rejected, ineligible, jev, agent, error), plus HTTP RED
  - Jev matched/declined/failed with the fake SDK, including tokens and
    latency
  - a scripted agent turn with a success, a grounding rejection, an
    unknown tool and an invalid argument: tool counters, tool durations,
    LLM durations and tokens, rounds, termination
  - `too_many_invalid_calls`, which has no event row
  - checkpointer operation timings and connection gauge against the real
    checkpointer
  - STT duration, audio length and RTF
  - the pool gauge and process CPU
  - a cardinality test that drives the app over HTTP (parser, patient
    search with a name in the query, a session id in the path, a full
    scripted agent turn) and asserts that no data point's attributes carry
    the session id, the request id, a patient name or an entity code, and
    that every attribute key is on an allowlist
- `tests/unit/test_telemetry.py` (3 tests): off by default; disabled
  installs nothing and recording is a no-op; enabled with no collector
  listening never raises, at setup, recording or shutdown.
- `tests/integration/test_voice_websocket.py::test_voice_turns_are_counted_with_channel_voice`.
- The `metric_reader` fixture (`tests/conftest.py`) rebinds the
  instruments to a fresh in-memory `MeterProvider` per test. OTel allows
  the process-global provider to be set only once, so it is never set in
  tests.
- Checked against the old code: with every `record_*` call-site helper
  stubbed to a no-op, 12 of the 13 new call-site tests fail (the gauges
  and telemetry-switch tests don't go through call sites). Also, swapping
  `unknown` back to the model's raw tool name makes the cardinality test
  fail on `made_up_tool_PT-1001`.

### Phase 4: Distributed tracing

One `/api/chat` request is now one connected trace, from the HTTP server
span down to each SQL statement. Spans are created through the OTel API
and exported only when `OTEL_ENABLED=true`, by the same switch as metrics.

| Piece | Where |
|---|---|
| Span helper (`span()`, error marking without messages) and export-side redaction (`RedactingSpanExporter`) | `backend/app/observability/spans.py` |
| `TracerProvider` + `BatchSpanProcessor` + OTLP/HTTP span exporter (`/v1/traces`); `LoggerProvider` + batch OTLP/HTTP log exporter (`/v1/logs`); auto-instrumentation | `backend/app/observability/telemetry.py` (now called at import in `app/main.py`; see below) |
| `trace_id`/`span_id` on log lines, OTLP log export | `backend/app/observability/otel_logs.py`, wired into `logging_config.py` |
| Manual spans | `chat.py`, `graph.py`, `voice.py`, `stt.py`, `checkpointer.py`; `request_id` on the root span from `request_context.py` |
| Dependencies: `opentelemetry-instrumentation-{fastapi,sqlalchemy,asyncpg,httpx}==0.66b0` (pulled in `-asgi`, `-instrumentation`, `util-http` 0.66b0, `wrapt` 2.5.0, `asgiref` 3.12.1) | `backend/requirements.txt` |

**The span tree of one agent chat request:**

```
POST /api/chat                      FastAPI auto (SERVER), root; request_id
└─ chat.handle                      chat.route, chat.channel, chat.handled_by, session.id
   ├─ parser.parse                  parser.matched, command.name
   ├─ domain_gate.check             domain_gate.in_domain
   ├─ eligibility.check             eligibility.eligible, eligibility.reason
   ├─ jev.consult                   jev.matched/choice/confidence/failure_reason/tokens (when enabled)
   └─ agent.run                     agent.rounds, agent.tool_calls, agent.termination
      ├─ checkpointer.put / .put_writes  (with a checkpointer; the initial
      │                                  get_tuple runs just before agent.run)
      ├─ agent.round                agent.round = 1
      │  ├─ llm.invoke              gen_ai.request.model, gen_ai.usage.*_tokens, llm.status
      │  │  └─ POST (httpx auto)    the provider API call, with traceparent
      │  ├─ tool.execute            tool.name, tool.status, tool.error_category
      │  │  └─ SELECT ... (SQLAlchemy auto) └─ asyncpg auto
      │  └─ tool.execute            e.g. grounding_rejected
      └─ agent.round                agent.round = 2
         └─ llm.invoke
```

A parser hit has `parser.parse` → `command.execute` (`command.name`,
`command.success`) instead of the gates and the agent. A voice utterance is
its **own trace**: `voice.utterance` (root; `session.id`, `request_id`,
`voice.audio_seconds`, plus a *link* to the WebSocket connection's server
span) → `stt.transcribe` (`stt.audio_seconds`, `stt.real_time_factor`,
`stt.status`) → the same `chat.handle` tree. One trace per connection would
grow without bound, so utterances link to the connection instead of being
its children.

**How to read one trace** (Grafana at <http://localhost:3001> → Explore →
Tempo, once Phase 5 runs the stack):
1. Find the trace by `request_id` (the `X-Request-ID` response header) or
   by `session.id` on `chat.handle`.
2. `chat.handle`'s `chat.route` says which path answered. If it is
   `agent`, the gate spans before `agent.run` say why the parser and Jev
   didn't (`parser.matched=false`, `jev.failure_reason=low_confidence`, ...).
3. Under `agent.run`, each `agent.round` is one model call plus the tools
   it requested. A wide `llm.invoke` is model latency. A wide
   `tool.execute` shows its SQL spans underneath, so DB time is visible.
   A `tool.error_category` of `grounding_rejected` is the model using an
   unseen code.
4. Copy the `trace_id` into Loki to get every log line of that request:
   `{service_name="hospital-ops-backend"} | trace_id="<id>"`. Verified in
   Phase 5 against Loki 3.7.8: the only index labels are `service_name`,
   `service_instance_id` and `deployment_environment_name` (promoted
   resource attributes); every other field of the log line (`trace_id`,
   `span_id`, `session_id`, `request_id`, `tool_name`, ...) is structured
   metadata, so it is filtered with `| key="value"` after the selector, not
   inside `{}`. In the other direction, every log line written inside a span
   has `trace_id`, and Grafana links it to Tempo.

**Design decisions:**
- **Rounds span two graph nodes.** A round's model call happens in
  `agent_node` and its tool calls in `tool_node`, and a context manager
  can't stay open across nodes. A small per-run holder (`_RoundSpans` in
  `graph.py`) starts `agent.round` in `agent_node`, and `tool_node` parents
  `tool.execute` on it explicitly. The next round, or `run_agent`'s
  `finally`, ends it. LangGraph runs each node in a task that copies the
  context, so `agent.round` correctly nests under `agent.run`; the tests
  check this rather than assume it.
- **`asyncio.to_thread` copies contextvars**, so `stt.transcribe` is the
  current span inside the faster-whisper worker thread. Verified in a test
  that reads the current span from inside the thread.
- **Rejections are not errors.** Span status ERROR is set for failures:
  LLM timeout, tool timeout or tool error, failed Jev call, and any
  exception escaping a span. Grounding, invalid-argument, unknown-tool and
  budget rejections are the system working as designed. They stay UNSET
  and are searchable by `tool.error_category`.
- **PHI on spans.** Call sites only set enums, counts, durations, the
  session id, fixed config values and entity-free command names. Never
  user text, transcripts, tool arguments or a model-invented tool name
  (`unknown` instead). Exceptions are recorded as the **type only**. A
  `RedactingSpanExporter` in front of the real exporter also covers spans
  we don't write ourselves: it strips query strings from `http.*`/`url.*`
  attributes (the ASGI instrumentation records
  `?query=<patient name>`), drops `url.query`, and removes
  `exception.message`/`exception.stacktrace` from events. FastAPI's
  instrumentation records those in full. SQL spans carry parameterized
  statements only: SQLAlchemy's commenter is off and asyncpg doesn't
  capture parameters.
- **FastAPI instrumentation is traces only** (a no-op `MeterProvider`), so
  `http.server.request.duration` is not double-counted. `/api/health` is
  excluded, and so are the per-message ASGI `receive`/`send` spans, which
  would add hundreds of spans per voice connection.
- **Setup moved from the lifespan to import time** in `app/main.py`.
  FastAPI instrumentation wraps the app's middleware stack, and Starlette
  builds that stack on the first ASGI call, which is the lifespan startup
  itself, so instrumenting there was too late. Shutdown still happens in
  the lifespan.
- **SQLAlchemy and asyncpg both produce a span per query.** That is
  redundant but cheap, and the nested pair separates ORM time from driver
  time. psycopg (the checkpointer) is not auto-instrumented; the manual
  `checkpointer.*` spans cover it.
- `agent_events` / `record_event` are unchanged. The trace panel and Tempo
  are different readers of overlapping facts.

**Log export to Loki.** The structlog chain is now:

```
merge_contextvars → add_trace_context → add_log_level → TimeStamper →
StackInfoRenderer → format_exc_info → redactor → export_to_otlp → JSONRenderer
```

`export_to_otlp` sits after the redactor, so the OTLP record (body = event
name, attributes = the remaining fields) is exactly the already-redacted
dict that is printed. Stdout output is unchanged apart from the new
`trace_id`/`span_id` fields. It is a no-op until `telemetry.py` binds a
logger, and it never raises. Why this and not a stdlib `logging` handler
bridge: there is no second formatting path that could drift from the
redaction. Honest caveats:
- The OTel Python logs API still lives in underscore modules
  (`opentelemetry._logs`, `opentelemetry.sdk._logs`), which upstream
  hasn't declared stable. Every use is confined to `otel_logs.py` and
  `telemetry.py`, and the SDK pin is narrow.
- stdlib-only loggers (uvicorn's access log) are not exported. The
  structured `request_completed` line covers the same requests.

**Tests** (`tests/integration/test_tracing.py`, 8 tests; in-memory span
exporter through the production `build_tracer_provider`, so through the
redacting exporter):
- A scripted agent chat over HTTP is one trace. It checks the parent chain
  `chat.handle → agent.run → agent.round → llm.invoke / tool.execute`, the
  gate spans under `chat.handle`, SQL auto-spans nested under
  `tool.execute`, a grounding rejection that is not an error, token and
  model attributes, and `agent.trace` log lines carrying the trace's
  `trace_id`.
- No span carries a patient's name: the `test_trace_redaction.py`
  scenario, with SQL instrumentation on.
- On a throwaway FastAPI app with real instrumentation: `request_id` on
  the root span, no query string, and no exception message on the error
  span.
- A voice utterance is its own trace, linked to the connection.
  `stt.transcribe` is current inside the worker thread, `chat.handle` is
  its child, and the transcript appears nowhere.
- `span()` errors record the type, never the message.
- Log lines inside a span carry its `trace_id`/`span_id`; lines outside
  carry neither.
- The OTLP log record is the redacted dict (`user_text: "[REDACTED]"`)
  with the span's trace id.
- Telemetry off: no recording spans, requests work, no `trace_id` on
  log lines.
- `tests/unit/test_telemetry.py` now also checks that enabling with no
  collector sets up all three signals and that recording a span and a log
  line, then shutting down, never raises. Measured cost: about 8 s of
  shutdown while the three exporters time out.
- `tests/conftest.py` forces `OTEL_ENABLED=false` at import, like the Jev
  fix, so a developer's `.env` can't make the suite export.
- Checked against the old code with pytest plugins that replace the new
  code with no-ops:
  - no manual spans and no log processors: 6 of 8 tests fail;
  - export-side redaction made a pass-through: the FastAPI
    query/exception test fails;
  - the `request_id` root-span line removed: the same test fails.
  The patient-name test passes without the redacting exporter, because
  the call sites themselves never put the name on a span. It guards the
  call sites, and the FastAPI test guards the exporter.

### Phase 5: The stack

One local container collects all three signals, and one checked-in dashboard
reads them. The dashboard was verified by running real, free, local traffic
through the real app and querying every panel through Grafana's HTTP API.

| Piece | Where |
|---|---|
| Stack: `grafana/otel-lgtm:0.34.0`, standalone compose project `hospital-ops-observability` | `observability/docker-compose.yml` |
| Dashboard provider (file provider, folder "Hospital Ops", UI edits not saved) | `observability/grafana/dashboards-provisioning.yaml` |
| The dashboard (uid `hospital-ops`), also Grafana's home dashboard | `observability/dashboards/hospital-ops.json` |
| Traffic generator | `observability/scripts/generate_traffic.py` |

**The image.** Pinned to `0.34.0`, the newest release on 2026-10-01
(Grafana 13.2.2, Prometheus 3.14.0, Tempo 3.0.3, Loki 3.7.8, OTel Collector
0.161.0), not `latest`: the provisioning path, the datasource UIDs and the
Prometheus name translation were all verified against this exact tag. Bump
it on purpose and re-run the verification below.

**Ports**, all bound to `127.0.0.1` only:

| Host | Container | What |
|---|---|---|
| `${GRAFANA_PORT:-3001}` | 3000 | Grafana. Host 3000 is taken by another local project |
| 4317 | 4317 | OTLP gRPC (unused by this app) |
| 4318 | 4318 | OTLP HTTP: the backend exports here (`OTEL_EXPORTER_OTLP_ENDPOINT` default) |

Prometheus (9090), Tempo (3200) and Loki (3100) are **not** published. Every
query goes through Grafana, either the UI or its datasource proxy
(`/api/datasources/proxy/uid/<uid>/...`).

**Provisioning**, using the image's documented "Add custom dashboards"
mechanism, as read-only mounts:
- `grafana/dashboards-provisioning.yaml` →
  `/otel-lgtm/grafana/conf/provisioning/dashboards/hospital-ops.yaml`, a
  file provider next to the image's own `grafana-dashboards.yaml`;
- `dashboards/` → `/otel-lgtm/grafana/conf/provisioning/dashboards/hospital-ops/`,
  the folder that provider reads.

Grafana rescans every 30 s, so an edited JSON file shows up without a
restart. The datasources are the image's own (`conf/provisioning/datasources/grafana-datasources.yaml`).
Their UIDs, read from the running Grafana's `/api/datasources`, are
`prometheus`, `tempo` and `loki`, and the dashboard uses those. The image
already links them:
- Prometheus exemplars (`trace_id`) → Tempo;
- Tempo → Loki "logs for this trace/span", with the query
  `{service_name="hospital-ops-backend"} | trace_id = "<id>"`;
- Loki → Tempo, a derived field on `trace_id`.

The Tempo → Loki query is exactly the query verified below. No datasource
change was needed, so none is mounted.

**Persistence.** A named volume `lgtm-data` is mounted at `/data`, where every
component writes (Prometheus TSDB, Tempo WAL and blocks, Loki chunks,
Grafana's database). Checked: after `docker compose restart` the counts were
unchanged: 204 `chat.handle` traces in Tempo, 892 log records in Loki, and
204 chat requests in Prometheus. `down -v` wipes it.

**One Prometheus flag.** `PROMETHEUS_EXTRA_ARGS=--enable-feature=created-timestamp-zero-ingestion`.
OTLP counters carry a start time, and with this flag Prometheus writes a
zero sample at that time. Without it, a counter series whose first sample
is already 1 (one `llm_timeout`, one dropped voice connection in a fresh
process) shows `increase()` = 0 until the event happens a second time, so
rare events would be invisible on the dashboard. Checked with a probe
counter: the series starts `0, 1` with the flag.

**Login.** Anonymous access is on, with the Admin role (the image's
default), so <http://localhost:3001> opens straight to the dashboard. The
`admin` / `admin` login also works. This is acceptable only because the
ports are bound to localhost.

#### Running it

```sh
# start (the image is ~0.9 GB on disk); ready when `docker logs hospital_ops_lgtm` says "up and running"
docker compose -f observability/docker-compose.yml up -d
# stop, keeping the data
docker compose -f observability/docker-compose.yml down
# stop and wipe all telemetry
docker compose -f observability/docker-compose.yml down -v
```

Different Grafana port: `GRAFANA_PORT=3002 docker compose -f observability/docker-compose.yml up -d`.

**Turning export on for the real app.** Set `OTEL_ENABLED=true` in
`backend/.env`, or in the environment, which wins over `.env`. Then start the
backend as usual (`uvicorn app.main:app`). The endpoint defaults to
`http://localhost:4318`, and metrics export every 15 s
(`OTEL_METRIC_EXPORT_INTERVAL_MS`). The setting is read when `app.main` is
imported, so restart the backend after changing it. With the stack down, the
app still works; exports just fail in the background. The test suite forces
`OTEL_ENABLED=false` whatever `.env` says.

#### The traffic generator

```sh
# from the repo root, stack up; ~1.5 min; prints session and request ids at the end
backend/.venv/Scripts/python.exe observability/scripts/generate_traffic.py
# options: --iterations N (default 3), --pause SECONDS (default 20), --no-voice, --endpoint URL
```

It runs the **real app in-process**: a Starlette `TestClient` entered as a
context manager, so the real lifespan builds the real checkpointer. The
WebSocket goes through the same client. Before `app.main` is imported, it
sets `OTEL_ENABLED=true` and `OTEL_METRIC_EXPORT_INTERVAL_MS=5000`. Every
request goes through the real middleware, routing, `CommandRunner`, agent
graph, tools, local faster-whisper STT and the dev database. Leaving the
client block runs the lifespan exit, which calls `shutdown_telemetry()` and
flushes all three signals before the process ends.

**No paid call can happen**, by construction:
- `ENABLE_JEV_FAST_PATH=false`, and `TYPESAFE_API_KEY`, `ANTHROPIC_API_KEY`
  and `OPENROUTER_API_KEY` are set to empty in the environment. The script
  asserts all of these before sending anything.
- `app.api.routes.chat.get_default_chat_model` is replaced by a queue of
  scripted models (`FakeMessagesListChatModel`, the
  `tests/integration/test_agent_loop.py` pattern). An agent turn with nothing
  queued raises instead of falling back.
- `app.agent.providers.factory.get_provider`, the only code that builds a
  real model, is patched to raise.
- Jev turns use a fake `typesafe_sdk` module in `sys.modules` (the
  `tests/conftest.py` pattern), with the flag turned on per request and a
  dummy key.
- `HF_HUB_OFFLINE=1`: the cached Whisper model is used without asking the
  Hub.

**It never calls `reschedule_appointment`**, the only mutating tool. Its
hospital-data access is read-only. It writes the normal per-session rows
(sessions, messages, agent events, checkpoints, grounding), all under
session ids `obs-traffic-<run>-<n><label>`.

Each iteration sends:
- **Parser and gates:** 5 parser hits, including `show patient Anthony
  Martin`, a real seeded name; 1 off-topic (`domain_rejected`); 1 over-long
  message (`ineligible`).
- **Operations:** departments, scanners, appointments, a patient search
  `?query=Anthony Martin`, a 404 on an unknown path, and `/api/cost-comparison`.
- **Jev, through the fake SDK:** matched; declined → agent; failed → agent.
- **Scripted agent turns:**
  - a 4-round search: appointments → scanners → `get_scanner_availability`
    on a grounded code → answer;
  - a grounding rejection (an unseen `SCN-7`);
  - an unknown tool plus an invalid argument;
  - `execute_command("show patient David Davis")`;
  - `too_many_invalid_calls`;
  - `max_rounds_exceeded` (`max_agent_rounds=2` for that turn);
  - `llm_timeout` (a model slower than a 1 s timeout);
  - a tool timeout (a real search with a 0 s budget);
  - a provider exception (`route=error`, HTTP 500).
- **Voice:** one connection streaming the WAV fixture twice. The transcript
  doesn't match the parser's word order and Jev is off, so each is a
  scripted agent turn with `channel=voice`. The connection is held open past
  one export so the open-connections gauge reads 1. Then one connection
  drops mid-utterance.

#### The dashboard: "Hospital Ops"

Every Prometheus query filters on `service_name="hospital-ops-backend"`.
Rates use `$__rate_interval`. "Totals" panels use `increase(...[$__range])`,
which Prometheus extrapolates to the window edges, so they read e.g. 3.08
for 3 events. Set the time range to cover the traffic.

| Row | Panel | Question it answers | Query (abridged) |
|---|---|---|---|
| API (RED) | Request rate by endpoint | How busy is each endpoint? | `sum by (http_route) (rate(http_server_request_duration_seconds_count[..]))` |
| | 5xx error share | What share of responses are server errors? | 5xx rate / all rate (`or vector(0)` so 0 shows as 0) |
| | /api/chat latency p50/p95/p99 | How long do chat requests take? Exemplar dots link to traces | `histogram_quantile(q, sum by (le) (rate(..._bucket{http_route="/api/chat"}[..])))` |
| | Responses by status code | Which status codes, and how many 4xx vs 5xx? | `sum by (http_response_status_code) (rate(..._count[..]))` |
| | Voice: open WebSocket connections | How many voice sessions are open? | `sum(voice_connections_active)` |
| | Voice: utterances by outcome | Do utterances reach routing or fail in STT/VAD? | `sum by (outcome) (increase(voice_utterances_total[$__range]))` |
| | Voice: dropped mid-utterance | How often is audio lost to a disconnect? | `sum(increase(voice_connections_dropped_total[$__range]))` |
| Routing mix | Parser hit rate / Jev hit rate / LLM fallback rate / Gate rejection rate / Chat turn errors / Voice share | What share of turns each path answered (LLM fallback = `route="agent"`) | `sum(increase(chat_requests_total{route=~".."}[$__range])) / sum(increase(chat_requests_total[$__range]))` |
| | Chat turns per second by route | Routing mix over time (stacked) | `sum by (route) (rate(chat_requests_total[..]))` |
| | Route share over time | The same, as shares stacking to 100% | per-route rate / `scalar(` total rate `)` |
| | Chat turns by route and channel | E.g. how many voice turns needed the agent | `sum by (route, channel) (increase(...))` |
| | Chat turn latency p95 by route | Parser ms vs agent seconds | `histogram_quantile(0.95, sum by (le, route) (rate(chat_request_duration_seconds_bucket[..])))` |
| Agent | Rounds per agent turn | Distribution of rounds; is the loop limit doing the stopping? | `sum by (le) (increase(agent_rounds_bucket[$__range]))`, heatmap format |
| | Agent terminations by type | How turns end: completed vs the four cut-offs | `sum by (termination) (increase(agent_terminations_total[..]))` |
| | Tool calls by tool and status | Which tools run, succeed, fail, get rejected | `sum by (tool_name, status) (increase(agent_tool_calls_total[..]))` |
| | Tool failures and rejections by category | Grounding rejections, invalid args, unknown tools, timeouts | `... {error_category!="none"}` by `error_category` |
| | LLM call latency p95 | Model latency, overall and per status | `histogram_quantile(0.95, ... llm_call_duration_seconds_bucket ...)` |
| | LLM calls per second by status | Model timeout and error rate | `sum by (status) (rate(llm_call_duration_seconds_count[..]))` |
| | LLM tokens | Token spend, by type and model | `sum by (token_type, model) (increase(llm_tokens_total[..]))` |
| | Tool latency p95 by tool | Which tool is slow (its SQL is in the trace) | `... agent_tool_duration_seconds_bucket` by `tool_name` |
| Jev | Consultations by outcome / latency p95 / tokens | How often Jev saves an agent turn, its latency and spend | `jev_consultations_total`, `jev_duration_seconds_bucket`, `jev_tokens_total` |
| Voice STT and CPU | STT duration p95 | Local Whisper latency per utterance | `... stt_duration_seconds_bucket` by `status` |
| | STT real-time factor | Is STT slower than speech (above the dashed line at 1)? | p95 of `stt_real_time_factor_bucket`, and mean = sum/count |
| | Process CPU (cores) | USE utilization of each backend process | `sum by (instance) (rate(process_cpu_time_seconds_total[..]))` |
| Database | SQLAlchemy pool connections | Pool saturation: checked out vs size | `clamp_min(max by (state) (db_pool_connections{pool="sqlalchemy"}), 0)` |
| | Checkpointer operation latency p95 | Checkpointer saturation (includes its lock wait) | `... checkpointer_operation_duration_seconds_bucket` by `operation` |
| | Checkpointer connection open | Is the checkpointer's connection up, per process? | `max by (instance) (checkpointer_connection_up_ratio)` |
| Traces and logs | Recent chat turns (Tempo) | Find a request; click its trace ID to open it, or to filter the logs below | TraceQL `{resource.service.name="hospital-ops-backend" && name="chat.handle"} \| select(span.chat.route, span.chat.channel)` |
| | Agent tool calls that were rejected or failed (Tempo) | What the agent tried that the system refused | TraceQL: `tool.execute` with `tool.error_category!="none"`, or `llm.status="timeout"` |
| | Backend logs (Loki) | The redacted log lines, all of them or one trace's (the `trace_id` box at the top) | `{service_name="hospital-ops-backend"} \| trace_id=~"${trace_id:regex}.*"` |
| SLOs: reserved for Phase 7 | (empty, collapsed) | Phase 7 adds 2–3 SLI/SLO panels here | |

Prometheus names, checked against the running stack's
`/api/v1/label/__name__/values`. The rules in Phase 3 hold: dots become
underscores, the unit is appended (`_seconds`), and counters get `_total`.
Two details the rules didn't spell out:
- `checkpointer.connection.up`, a gauge with unit `1`, becomes
  **`checkpointer_connection_up_ratio`**.
- The `stt.real_time_factor` and `agent.rounds` histograms get no unit
  suffix.

Attribute names become labels the same way (`http.route` → `http_route`).
`service.name`, `service.instance.id`, `service.version` and
`deployment.environment.name` are promoted to labels. Every backend process
gets a new `service_instance_id` (also `instance`). That is why the gauges
aggregate with `max by` and the CPU and checkpointer panels show one line
per process.

#### Investigating: metric → trace → logs

The walkthrough for "why was this chat slow / why did it go to Sonnet":
1. **Metric.** On *Chat turn latency p95 by route* or */api/chat latency*,
   hover an exemplar dot near the spike. It carries the `trace_id` of one
   real request in that bucket; click "Query with Tempo". Without
   exemplars, use *Recent chat turns* and pick a turn by its route.
2. **Trace.** The span tree shows where the time went: the gate spans
   (`parser.matched=false`, `domain_gate.in_domain`), `agent.run`
   (`agent.rounds`, `agent.termination`), each `agent.round` with its
   `llm.invoke` and `tool.execute`, SQL under each tool, and the
   `checkpointer.*` spans. For refusals, start from *Agent tool calls that
   were rejected or failed*.
3. **Logs.** In the trace view, "Logs for this span" opens Loki with that
   trace's lines. On the dashboard, click "Show this trace's logs below" on
   a trace ID to fill the `trace_id` box. Every line also carries
   `session_id` and `request_id`, so you can widen from one request to the
   whole session: `{service_name="hospital-ops-backend"} | session_id="<id>"`.

The unknown-unknowns case: a question nobody built a panel for (say, "do
grounding rejections cluster in one session?") can still be answered from
spans and logs that already exist, with TraceQL or LogQL in Explore. No
new instrumentation is needed. That is the difference between this and
monitoring.

#### Verification (2026-10-01, stack 0.34.0)

Run: `generate_traffic.py` with defaults (3 iterations, 20 s apart), then
every panel's query was sent to Grafana's `/api/ds/query` on port 3001, with
the time range covering the run. **No panel was empty.** Exact counter
values for that run's process (read with `max_over_time` on its `instance`,
so no extrapolation):

| Metric | Result |
|---|---|
| `chat_requests_total` | parser 15, domain_rejected 3, ineligible 3, jev 3, agent 30 (text) + 6 (voice), error 3 |
| `http_server_request_duration_seconds_count` | `/api/chat` 200 ×54 and 500 ×3; each operations route and `/api/cost-comparison` 200 ×3; `unmatched` 404 ×3 |
| `agent_terminations_total` | completed 27, too_many_invalid_calls 3, max_rounds_exceeded 3, llm_timeout 3 |
| `agent_tool_calls_total` | search_appointments success 15 / failure (timeout) 3; search_scanners 6; get_scanner_availability success 3, grounding_rejected 3, invalid_argument 3; execute_command 3; unknown/unknown_tool 12 |
| `llm_call_duration_seconds_count` | ok 66, timeout 3, error 3. `llm_tokens_total`: input 82,500, output 2,190 |
| `jev_consultations_total` | matched 3, declined 3, failed 3. `jev_tokens_total`: input 2,100, output 30 (fake SDK) |
| `voice_utterances_total` | routed 6. `voice_connections_dropped_total`: 3. `voice_connections_active` peaked at 1 |
| `stt_duration_seconds` | 6 utterances, mean 0.68 s, p95 ≈ 2.05 s (the first includes model load); mean real-time factor 0.18 |
| `checkpointer_operation_duration_seconds_count` | get_tuple 114, put 207, put_writes 153. `checkpointer_connection_up_ratio`: 1 |
| `db_pool_connections` | size 5, checked_out 0 (between requests) |
| Derived (dashboard stats) | parser hit rate 23.8%, LLM fallback 57.1%, Jev 4.8%, gate rejections 9.5%, errors 4.8%, voice 9.5%, 5xx share 4.0% |

Exemplars: `http_server_request_duration_seconds`,
`chat_request_duration_seconds` and `llm_call_duration_seconds` all carry
`trace_id`/`span_id` exemplars (91, 69 and 23 in the first hour).

**One trace, end to end.** The 4-round search turn, `request_id`
`3fca22c6f697470ab4c191991b572618`, is found in Tempo with
`{span.request_id="3fca22c6..."}` → trace `b094f638c029087832d1a9c5b28ab59e`,
200 spans:

```
POST /api/chat [1081 ms]  request_id=3fca22c6...
└─ chat.handle [1079 ms]  chat.route=agent, chat.channel=text, session.id=obs-traffic-9e4bbd-32multi-roun
   ├─ domain_gate.check (in_domain=true) · parser.parse (matched=false) · eligibility.check (eligible=true)
   ├─ checkpointer.get_tuple, SQL
   └─ agent.run [872 ms]  agent.rounds=4, agent.termination=completed
      ├─ agent.round 1 → llm.invoke (ok, 1200 input tokens) → tool.execute search_appointments (success) → SQL
      ├─ agent.round 2 → llm.invoke (ok)                    → tool.execute search_scanners (success) → SQL
      ├─ agent.round 3 → llm.invoke (ok)                    → tool.execute get_scanner_availability (success) → SQL
      ├─ agent.round 4 → llm.invoke (ok, final answer)
      └─ checkpointer.put / put_writes (×n), agent_events INSERTs
```

Loki, `{service_name="hospital-ops-backend"} | trace_id="b094f638c029087832d1a9c5b28ab59e"`,
returns **12 lines**: `agent_invoked`, `llm_response` and `tool_executed`
for rounds 1–3, then `agent_invoked` and `llm_response` for round 4, then
`request_completed` (`route=/api/chat`). Every line carries the same
`trace_id`, `request_id` and `session_id`. The dashboard's log panel, with
the `trace_id` box set to that id, returns the same 12 lines.

**PHI check.** Each traffic run sent two real seeded names, each three
times:
- `Anthony Martin`, as a parser `show patient` and as the patient-search
  query string;
- `David Davis`, inside an agent `execute_command`.

After two full runs, the check fetched in full every trace Grafana's ranged
Tempo query returned for the service over the last 2 h: **365 traces,
22,721 spans**. That set includes all **24** requests that carried a name,
each looked up by `request_id`. It also fetched every Loki record in the
window with its structured metadata: **1,181 records**. All of it was
searched, case-insensitively, for `Anthony Martin`, `David Davis`, their
`+` and `%20` URL forms, and the bare surnames `Martin` and `Davis`.
**0 hits in both.** As a control, the patient-search span keeps
`http.target` and `http.url` as `/api/operations/patients` with the query
string stripped, and its `request_completed` line carries only the route.
(Tempo's `/api/search` without `start`/`end` covers only recent traces.
Enumerate through Grafana's `/api/ds/query`, which sends the time range.)

#### Findings from verification (reported, not fixed in Phase 5)

- **`db.pool.connections{state="overflow"}` goes negative.** The gauge
  reports SQLAlchemy's raw `QueuePool.overflow()`, which is `_overflow`. That
  value starts at `-pool_size` and rises as connections are opened, so it
  read **-4** with a pool of 5. The panel clamps at 0. The fix belongs in
  `metrics.observe_sqlalchemy_pool`: report `max(0, pool.overflow())`.
- **Agent log lines carry `agent.run`'s `span_id`**, not the `agent.round` or
  `tool.execute` span they describe. Trace-level linking works fully. Span-level
  "logs for this span" on a `tool.execute` span finds nothing.
- **The last `agent.round` span includes post-round work.** It is ended by
  `run_agent`'s `finally`, so round 4 above lasts 784 ms although its
  `llm.invoke` took ~0 ms. The time is the final checkpoint writes. Read the
  last round's width with that in mind.

## 3. Decisions

- **Stack location: `observability/docker-compose.yml`, separate from the
  root compose file (final, Phase 5).** The app must run with telemetry
  absent, so the stack is opt-in rather than a dependency of `postgres`:
  `docker compose up` at the root never starts it, and stopping it never
  touches the database container. It has its own compose project name
  (`hospital-ops-observability`) and its own volume, so `down -v` on it
  wipes only telemetry. It is one container (`grafana/otel-lgtm`) rather
  than five separate services because, at this scale, the bundled
  Collector → Prometheus/Tempo/Loki → Grafana wiring and its pre-linked
  datasources are exactly what is needed, and the image's documented
  extension points (a `/data` volume, a dashboard provider file,
  `*_EXTRA_ARGS`) cover every customization made here. Its ports are bound
  to `127.0.0.1` only, because Grafana runs with anonymous Admin access.
- **`agent_events` stays as it is.** It is a product feature (the UI trace
  panel). OTel adds operator-facing telemetry alongside it and replaces
  nothing.
- **Metric labels.** Labels are low-cardinality only. Per-request identifiers
  go on spans and log lines.

# Observability Concept Map

Maps the 10-module observability curriculum onto this codebase. Each entry
gives what the concept is, where it is implemented, and how to see it
working. This is the same idea as `docs/CONCEPT_COVERAGE.md` for the
original 58 concepts.

The **Fit** column carries over the original audit's grading:

- **Core**: used directly here.
- **Light**: a small, deliberate amount.
- **Theory**: understood, but deliberately not built at this project's scale.

**Status** is Done, Planned (with the phase number), or Not built (by design).
Entries are updated as each phase lands.

## Module 1: Fundamentals

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Observability vs monitoring | Monitoring answers known questions; observability lets you ask new ones of telemetry you already have | Monitoring = the fixed panels of the "Hospital Ops" dashboard (<http://localhost:3001>), each answering a question decided in advance. Observability = the "Traces and logs" row and Explore: drill from a panel into one request's trace and logs, or ask a TraceQL/LogQL question no panel was built for. README §2 Phase 5, "Investigating" | Core | Done (5) |
| White-box instrumentation | Telemetry emitted from inside the code | `app/observability/*` and the call sites in `chat.py` / `graph.py` | Core | Partial (logs + trace + correlation) |
| Unknown-unknowns debugging | Investigating failures nobody wrote an alert for, e.g. "why did this go to Sonnet?" | One trace per request: the gate spans say why it reached `agent.run` (`parser.matched=false`, `jev.failure_reason`). See it: dashboard panel "Agent tool calls that were rejected or failed (Tempo)" (TraceQL on `tool.error_category`), or "Recent chat turns" → a trace with `chat.route=agent` → its gate spans | Core | Done (4/5) |

## Module 2: Logs, metrics, traces

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Logs | Discrete events with context | structlog JSON on stdout (`logging_config.py`) | Core | Done (pre-existing) |
| Metrics | Numeric time series, cheap to aggregate | `app/observability/metrics.py` (instruments), `telemetry.py` (SDK + OTLP export). See it: `pytest tests/integration/test_metrics.py -v` | Core | Done (3) |
| Traces / spans | One request's causal tree | `app/observability/spans.py` + `telemetry.py`. See it: `pytest tests/integration/test_tracing.py -v` | Core | Done (4) |

## Module 3: Structured logging

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Structured JSON events | Machine-parseable log lines | `logging_config.py` `JSONRenderer` | Core | Done (pre-existing) |
| Contextual logging | Context bound once, carried on every line | `request_id` bound by the `request_context.py` middleware; `session_id` bound in `handle_chat_message` and the voice handler; merged by `merge_contextvars`. See it: every JSON log line of a chat turn has both | Core | Done (2) |
| Request / correlation ID | Ties every line of one request together | `app/observability/request_context.py`. See it: `curl -i` any endpoint; the `X-Request-ID` header matches `request_id` on every line it printed. `pytest tests/e2e/test_request_correlation.py -v` | Core | Done (2) |
| Redaction / no PHI or secrets | Logs must never carry patient data or credentials | `app/observability/redaction.py`: structlog processor + uvicorn access-log filter. See it with `pytest tests/unit/test_redaction.py -v` | Core | Done (1) |
| Sampling, rotation, retention | Volume control | n/a at this traffic level | Light | Not built (by design) |

## Module 4: Metrics

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Counter | Monotonic count; graph its rate | `chat.requests{route,channel}` (`chat_requests_total` in Prometheus), `agent.tool.calls`, `jev.consultations` | Core | Done (3) |
| Histogram / percentiles | Latency distribution → p50/p95/p99 | `http.server.request.duration`, `chat.request.duration`, `llm.call.duration`, `agent.tool.duration`, `stt.duration`, `jev.duration`, all in seconds with explicit buckets | Core | Done (3) |
| Gauge | A current value | `db.pool.connections{state}` (observable gauge), `voice.connections.active` (up-down counter), `checkpointer.connection.up` (`checkpointer_connection_up_ratio` in Prometheus). See it: panels "SQLAlchemy pool connections", "Voice: open WebSocket connections", "Checkpointer connection open" | Core | Done (3) |
| RED | Rate, Errors, Duration per endpoint | `http.server.request.duration{http.request.method, http.route, http.response.status_code}` from the request middleware, plus the `request_completed` log line; voice: `voice.connections.active`, `voice.utterances{outcome}`, `voice.connections.dropped`. See it: dashboard row "API: rate, errors, duration (RED)" | Core | Done (2/3/5) |
| Labels and cardinality | Labels multiply series; IDs as labels explode them | Fixed attribute sets only; model-invented tool names become `unknown`. See it: `test_no_metric_attribute_carries_ids_codes_or_free_text` | Core | Done (3) |
| Business metrics | Domain outcomes | Routing mix: `chat.requests{route}`; reschedules: `agent.tool.calls{tool_name="reschedule_appointment",status="success"}` | Core | Done (3) |
| Throughput / queue depth | | No queue in this system | Theory | Not built (by design) |

## Module 5: Distributed tracing

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Parent/child spans | The causal tree of one request | `POST /api/chat` → `chat.handle` → gates / `jev.consult` → `agent.run` → `agent.round` → `llm.invoke` / `tool.execute` → SQL. Tree in README §2 Phase 4 | Core | Done (4) |
| Span attributes / events / status | Searchable context and error state on a span | `chat.route`, `tool.name`, `tool.error_category`, token counts; ERROR for failures and timeouts only; exceptions recorded as type only | Core | Done (4) |
| Async context propagation | Context surviving `await` and task boundaries | Through LangGraph's node tasks (`agent.round` under `agent.run`), `asyncio.wait_for` (SQL under `tool.execute`) and `asyncio.to_thread` (STT thread inside `stt.transcribe`), all asserted in `test_tracing.py`. Rounds that span two nodes are parented explicitly (`_RoundSpans`) | Core | Done (4) |
| Request vs trace vs session ID | Three different scopes of identity | `request_id` = one HTTP call or WebSocket connection (log field + root span attribute); `trace_id` = one request's span tree, or one voice utterance; `session.id` = LangGraph thread, across many traces | Core | Done (2/4) |
| Cross-service propagation | `traceparent` across HTTP hops | httpx auto-instrumentation: the LLM provider call is a CLIENT span under `llm.invoke` and carries `traceparent` (Jev too, if its SDK uses httpx) | Light | Done (4) |
| Head / tail sampling | Dropping traces to save cost | 100% kept at this volume | Theory | Not built (by design) |

## Module 6: OpenTelemetry

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| API vs SDK, Tracer/MeterProvider | Instrument against the API; the SDK decides export | `metrics.py`/`spans.py`/`otel_logs.py` use the API (no-op by default); `telemetry.py` installs the SDK Meter/Tracer/LoggerProviders only when `OTEL_ENABLED=true`. See it: `tests/unit/test_telemetry.py` | Core | Done (3/4) |
| Auto vs manual instrumentation | Library hooks vs explicit spans | Auto: FastAPI (traces only), SQLAlchemy, asyncpg, httpx in `telemetry.instrument_libraries`. Manual: the request path via `spans.span()` | Core | Done (4) |
| Resource attributes | `service.name` etc. | `telemetry.build_resource`: `service.name=hospital-ops-backend`, `service.version`, `deployment.environment.name` | Core | Done (3) |
| OTLP exporter + Collector | Wire protocol and the routing hop | The app pushes OTLP/HTTP to `localhost:4318`; the Collector inside `grafana/otel-lgtm:0.34.0` (`observability/docker-compose.yml`) fans out to Prometheus (its OTLP receiver), Tempo and Loki. See it: run `observability/scripts/generate_traffic.py`, then any dashboard panel; the Collector's own counters (`otelcol_receiver_accepted_spans_total`, `otelcol_exporter_sent_spans_total`) are in Explore → Prometheus | Core | Done (5) |
| Collector processor pipelines | | Image defaults are enough here | Theory | Not built (by design) |

## Module 7: Backend, infra, Postgres, async

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Connection pool saturation | Pool exhaustion queues every request | SQLAlchemy `QueuePool`: `db.pool.connections{state=size/checked_out/overflow}`. The checkpointer is one psycopg connection behind a lock, not a pool: its saturation is `checkpointer.operation.duration` (lock wait included), plus `checkpointer.connection.up` | Core | Done (3) |
| DB query latency | | SQLAlchemy + asyncpg spans (parameterized statements, never values), nested under the `tool.execute` that ran them; `checkpointer.*` spans for psycopg | Core | Done (4) |
| USE (CPU) | Utilization/Saturation/Errors of a resource | Utilization: `rate(process.cpu.time)`; saturation: `stt.real_time_factor` > 1; errors: `stt.duration{status="error"}`. See it: row "Voice STT and CPU (USE)" (the RTF panel draws the line at 1) | Core | Done (3/5) |
| Event-loop blocking | CPU work stalling every other request | Compare `stt.duration` with `http.server.request.duration` during voice use (STT runs in `asyncio.to_thread`, so it should not stall the loop). See it: "STT duration p95" next to "/api/chat latency" over the same range (two panels, not a dual axis) | Core | Done (3/5) |
| Timeouts | | `llm.call.duration{status="timeout"}`, `agent.tool.calls{error_category="timeout"}`, `agent.terminations{termination="llm_timeout"}` | Core | Done (3) |
| Locks / deadlocks / retry storms | | One write path, no retries | Theory | Not built (by design) |

## Module 8: Errors and SLOs

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Error categories | validation / dependency / programming | `error_category` on `agent.tool.calls`; `outcome=failed` on `jev.consultations`; `route=error` on `chat.requests` | Core | Done (3) |
| SLI / SLO | Measurement / target | Three SLOs in `observability/slos.yaml` (fast-path latency, agent-turn latency, chat availability), rendered by `scripts/build_slo_panels.py`. See it: dashboard row "SLOs", the SLI stat per SLO (green = met) | Light | Done (7) |
| Error budget | Allowed failure under the SLO: `1 - (1 - SLI) / (1 - target)` | "error budget left" stat per SLO. Chat availability shows it spent (negative) on the verification traffic, which injects 500s | Light | Done (7) |
| Burn-rate multi-window alerts, severity tiers | | Overkill for one developer | Theory | Not built (by design) |
| SLA | Contractual commitment | No customers | Theory | Not built (by design) |

## Module 9: Dashboards, cost, security, audit

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Dashboard as code | Reproducible dashboards | `observability/dashboards/hospital-ops.json`, provisioned read-only by `observability/grafana/dashboards-provisioning.yaml` (UI edits are not saved; edit the file, Grafana reloads it within 30 s). See it: `docker compose -f observability/docker-compose.yml up -d`, open <http://localhost:3001>; the dashboard is the home page | Core | Done (5) |
| Metric → trace → logs | The investigation path | Metric → trace: exemplars (`trace_id`) on the latency histograms, shown as dots on "/api/chat latency" and "Chat turn latency p95 by route", linked to Tempo. Trace → logs: Tempo's "Logs for this span" (`{service_name="hospital-ops-backend"} \| trace_id="<id>"`) and the dashboard's "Show this trace's logs below" link into "Backend logs (Loki)". Logs → trace: Loki's `trace_id` derived field. Walkthrough and a verified example: README §2 Phase 5 | Core | Done (4/5) |
| PHI redaction / allowlisting | Store only fields proven safe, not everything except known-bad ones | `redaction.redact_arguments` on every `agent_events` write. See it: trace panel shows `command_text: "[REDACTED]"` after a `show patient` tool call; `tests/integration/test_trace_redaction.py` | Core | Done (1) |
| Secrets out of logs | Credentials never in telemetry | `redaction.make_log_redactor` scrubs API keys + DB password from every field incl. tracebacks; `hide_parameters=True` on the engine | Core | Done (1) |
| Observability vs audit logs | Telemetry is redacted, sampled and lossy; an audit record is complete, immutable, who/what/before/after | `audit_log` table, written in the same transaction as `reassign_scanner`, append-only via DB trigger. See it: `SELECT * FROM audit_log`, then follow its `trace_id` into Tempo | Core | Done (6) |
| Telemetry cost / retention tuning | | n/a locally | Theory | Not built (by design) |

## Module 10: LLM and agent observability

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Action trace without chain-of-thought | Record decisions, not reasoning | `tracing.py` + `agent_events` + trace panel | Core | Done (pre-existing) |
| Parser hit rate / LLM fallback rate | Share of requests answered without Sonnet | `chat.requests{route}`: parser share = `route="parser"` / all. See it: stat panels "Parser hit rate" and "LLM fallback rate" (row "Routing mix") | Core | Done (3/5) |
| Jev consultation telemetry | Confidence, tokens, latency, outcome | `jev_invoked` events + `jev.consultations{outcome}`, `jev.duration`, `jev.tokens{token_type}` | Core | Done (3) |
| LLM latency / tokens / cost | | `llm.call.duration{status,model}`, `llm.tokens{token_type,model}`; cost stays in `/api/cost-comparison` | Core | Done (3) |
| Tool latency / failure / invalid calls | | events + `agent.tool.calls{tool_name,status,error_category}`, `agent.tool.duration` | Core | Done (3) |
| Grounding rejection rate | | `agent.tool.calls{error_category="grounding_rejected"}` | Core | Done (3) |
| Agent rounds / loop limits / timeouts | | `agent.rounds` histogram, `agent.terminations{termination}` (all five, including the two with no event row) | Core | Done (3) |

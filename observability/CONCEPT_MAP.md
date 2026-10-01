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
| Observability vs monitoring | Monitoring answers known questions; observability lets you ask new ones of telemetry you already have | The whole folder. Monitoring = dashboard panels; observability = drilling from a panel into one request's trace and logs | Core | Planned (5) |
| White-box instrumentation | Telemetry emitted from inside the code | `app/observability/*` and the call sites in `chat.py` / `graph.py` | Core | Partial (logs + trace + correlation) |
| Unknown-unknowns debugging | Investigating failures nobody wrote an alert for, e.g. "why did this go to Sonnet?" | Grafana Explore → Tempo trace for one request | Core | Planned (4/5) |

## Module 2: Logs, metrics, traces

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Logs | Discrete events with context | structlog JSON on stdout (`logging_config.py`) | Core | Done (pre-existing) |
| Metrics | Numeric time series, cheap to aggregate | `app/observability/metrics.py` (instruments), `telemetry.py` (SDK + OTLP export). See it: `pytest tests/integration/test_metrics.py -v` | Core | Done (3) |
| Traces / spans | One request's causal tree | OTel tracer (Phase 4) | Core | Planned (4) |

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
| Gauge | A current value | `db.pool.connections{state}` (observable gauge), `voice.connections.active` (up-down counter), `checkpointer.connection.up` | Core | Done (3) |
| RED | Rate, Errors, Duration per endpoint | `http.server.request.duration{http.request.method, http.route, http.response.status_code}` from the request middleware, plus the `request_completed` log line; voice: `voice.connections.active`, `voice.utterances{outcome}`, `voice.connections.dropped` | Core | Done (2/3; panels in 5) |
| Labels and cardinality | Labels multiply series; IDs as labels explode them | Fixed attribute sets only; model-invented tool names become `unknown`. See it: `test_no_metric_attribute_carries_ids_codes_or_free_text` | Core | Done (3) |
| Business metrics | Domain outcomes | Routing mix: `chat.requests{route}`; reschedules: `agent.tool.calls{tool_name="reschedule_appointment",status="success"}` | Core | Done (3) |
| Throughput / queue depth | | No queue in this system | Theory | Not built (by design) |

## Module 5: Distributed tracing

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Parent/child spans | The causal tree of one request | parser → domain gate → Jev → agent round → tool → DB | Core | Planned (4) |
| Span attributes / events / status | Searchable context and error state on a span | route, tool name, grounding outcome | Core | Planned (4) |
| Async context propagation | Context surviving `await` and task boundaries | OTel contextvars through the LangGraph loop | Core | Planned (4) |
| Request vs trace vs session ID | Three different scopes of identity | Request = one HTTP call or one WebSocket connection (`request_id`); session = LangGraph thread (`session_id`, spans many requests); trace id comes in Phase 4 | Core | Partial (2: request + session; trace 4) |
| Cross-service propagation | `traceparent` across HTTP hops | Outbound LLM/Jev calls only, via httpx instrumentation | Light | Planned (4) |
| Head / tail sampling | Dropping traces to save cost | 100% kept at this volume | Theory | Not built (by design) |

## Module 6: OpenTelemetry

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| API vs SDK, Tracer/MeterProvider | Instrument against the API; the SDK decides export | `metrics.py` records via the API (no-op by default); `telemetry.py` installs the SDK `MeterProvider` only when `OTEL_ENABLED=true`. See it: `tests/unit/test_telemetry.py` | Core | Done for metrics (3); tracer (4) |
| Auto vs manual instrumentation | Library hooks vs explicit spans | FastAPI/SQLAlchemy/asyncpg/httpx auto; request path manual | Core | Planned (4) |
| Resource attributes | `service.name` etc. | `telemetry.build_resource`: `service.name=hospital-ops-backend`, `service.version`, `deployment.environment.name` | Core | Done (3) |
| OTLP exporter + Collector | Wire protocol and the routing hop | `grafana/otel-lgtm` bundles the Collector | Core | Planned (5) |
| Collector processor pipelines | | Image defaults are enough here | Theory | Not built (by design) |

## Module 7: Backend, infra, Postgres, async

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Connection pool saturation | Pool exhaustion queues every request | SQLAlchemy `QueuePool`: `db.pool.connections{state=size/checked_out/overflow}`. The checkpointer is one psycopg connection behind a lock, not a pool: its saturation is `checkpointer.operation.duration` (lock wait included), plus `checkpointer.connection.up` | Core | Done (3) |
| DB query latency | | SQLAlchemy/asyncpg auto-instrumentation spans | Core | Planned (4) |
| USE (CPU) | Utilization/Saturation/Errors of a resource | Utilization: `rate(process.cpu.time)`; saturation: `stt.real_time_factor` > 1; errors: `stt.duration{status="error"}` | Core | Done (3; panels in 5) |
| Event-loop blocking | CPU work stalling every other request | Compare `stt.duration` with `http.server.request.duration` during voice use (STT runs in `asyncio.to_thread`, so it should not stall the loop) | Core | Done (3; panel in 5) |
| Timeouts | | `llm.call.duration{status="timeout"}`, `agent.tool.calls{error_category="timeout"}`, `agent.terminations{termination="llm_timeout"}` | Core | Done (3) |
| Locks / deadlocks / retry storms | | One write path, no retries | Theory | Not built (by design) |

## Module 8: Errors and SLOs

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Error categories | validation / dependency / programming | `error_category` on `agent.tool.calls`; `outcome=failed` on `jev.consultations`; `route=error` on `chat.requests` | Core | Done (3) |
| SLI / SLO | Measurement / target | 2–3 dashboard panels | Light | Planned (7) |
| Error budget | Allowed failure under the SLO | SLO panels | Light | Planned (7) |
| Burn-rate multi-window alerts, severity tiers | | Overkill for one developer | Theory | Not built (by design) |
| SLA | Contractual commitment | No customers | Theory | Not built (by design) |

## Module 9: Dashboards, cost, security, audit

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Dashboard as code | Reproducible dashboards | `observability/dashboards/*.json` | Core | Planned (5) |
| Metric → trace → logs | The investigation path | Grafana exemplars → Tempo → Loki via `trace_id` | Core | Planned (4/5) |
| PHI redaction / allowlisting | Store only fields proven safe, not everything except known-bad ones | `redaction.redact_arguments` on every `agent_events` write. See it: trace panel shows `command_text: "[REDACTED]"` after a `show patient` tool call; `tests/integration/test_trace_redaction.py` | Core | Done (1) |
| Secrets out of logs | Credentials never in telemetry | `redaction.make_log_redactor` scrubs API keys + DB password from every field incl. tracebacks; `hide_parameters=True` on the engine | Core | Done (1) |
| Observability vs audit logs | Sampled, operator-facing vs complete, who/what/before/after | `reschedule_appointment` audit table | Core | Planned (6) |
| Telemetry cost / retention tuning | | n/a locally | Theory | Not built (by design) |

## Module 10: LLM and agent observability

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Action trace without chain-of-thought | Record decisions, not reasoning | `tracing.py` + `agent_events` + trace panel | Core | Done (pre-existing) |
| Parser hit rate / LLM fallback rate | Share of requests answered without Sonnet | `chat.requests{route}`: parser share = `route="parser"` / all | Core | Done (3) |
| Jev consultation telemetry | Confidence, tokens, latency, outcome | `jev_invoked` events + `jev.consultations{outcome}`, `jev.duration`, `jev.tokens{token_type}` | Core | Done (3) |
| LLM latency / tokens / cost | | `llm.call.duration{status,model}`, `llm.tokens{token_type,model}`; cost stays in `/api/cost-comparison` | Core | Done (3) |
| Tool latency / failure / invalid calls | | events + `agent.tool.calls{tool_name,status,error_category}`, `agent.tool.duration` | Core | Done (3) |
| Grounding rejection rate | | `agent.tool.calls{error_category="grounding_rejected"}` | Core | Done (3) |
| Agent rounds / loop limits / timeouts | | `agent.rounds` histogram, `agent.terminations{termination}` (all five, including the two with no event row) | Core | Done (3) |

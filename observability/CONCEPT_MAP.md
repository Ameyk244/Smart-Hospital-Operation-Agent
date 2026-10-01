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
| White-box instrumentation | Telemetry emitted from inside the code | `app/observability/*` and the call sites in `chat.py` / `graph.py` | Core | Partial (existing logs + trace) |
| Unknown-unknowns debugging | Investigating failures nobody wrote an alert for, e.g. "why did this go to Sonnet?" | Grafana Explore → Tempo trace for one request | Core | Planned (4/5) |

## Module 2: Logs, metrics, traces

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Logs | Discrete events with context | structlog JSON on stdout (`logging_config.py`) | Core | Done (pre-existing) |
| Metrics | Numeric time series, cheap to aggregate | OTel meter (Phase 3) | Core | Planned (3) |
| Traces / spans | One request's causal tree | OTel tracer (Phase 4) | Core | Planned (4) |

## Module 3: Structured logging

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Structured JSON events | Machine-parseable log lines | `logging_config.py` `JSONRenderer` | Core | Done (pre-existing) |
| Contextual logging | Context bound once, carried on every line | `structlog.contextvars.merge_contextvars` (already configured; Phase 2 binds values) | Core | Planned (2) |
| Request / correlation ID | Ties every line of one request together | Request-ID middleware | Core | Planned (2) |
| Redaction / no PHI or secrets | Logs must never carry patient data or credentials | `app/observability/redaction.py` | Core | Planned (1) |
| Sampling, rotation, retention | Volume control | n/a at this traffic level | Light | Not built (by design) |

## Module 4: Metrics

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Counter | Monotonic count; graph its rate | `chat_requests_total{route}` | Core | Planned (3) |
| Histogram / percentiles | Latency distribution → p50/p95/p99 | request, LLM, tool, STT duration histograms | Core | Planned (3) |
| Gauge | A current value | DB pool checked-out connections | Core | Planned (3) |
| RED | Rate, Errors, Duration per endpoint | `/api/chat` + voice WebSocket | Core | Planned (3/5) |
| Labels and cardinality | Labels multiply series; IDs as labels explode them | Only `route`/`tool_name`/`status`/`error_category`; never `session_id` or codes | Core | Planned (3) |
| Business metrics | Domain outcomes | Routing mix (parser/Jev/agent), reschedules | Core | Planned (3) |
| Throughput / queue depth | | No queue in this system | Theory | Not built (by design) |

## Module 5: Distributed tracing

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Parent/child spans | The causal tree of one request | parser → domain gate → Jev → agent round → tool → DB | Core | Planned (4) |
| Span attributes / events / status | Searchable context and error state on a span | route, tool name, grounding outcome | Core | Planned (4) |
| Async context propagation | Context surviving `await` and task boundaries | OTel contextvars through the LangGraph loop | Core | Planned (4) |
| Request vs trace vs session ID | Three different scopes of identity | Request/trace = one HTTP call; session = LangGraph thread | Core | Planned (2/4) |
| Cross-service propagation | `traceparent` across HTTP hops | Outbound LLM/Jev calls only, via httpx instrumentation | Light | Planned (4) |
| Head / tail sampling | Dropping traces to save cost | 100% kept at this volume | Theory | Not built (by design) |

## Module 6: OpenTelemetry

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| API vs SDK, Tracer/MeterProvider | Instrument against the API; the SDK decides export | `app/observability/` OTel setup | Core | Planned (3/4) |
| Auto vs manual instrumentation | Library hooks vs explicit spans | FastAPI/SQLAlchemy/asyncpg/httpx auto; request path manual | Core | Planned (4) |
| Resource attributes | `service.name` etc. | OTel setup | Core | Planned (3) |
| OTLP exporter + Collector | Wire protocol and the routing hop | `grafana/otel-lgtm` bundles the Collector | Core | Planned (5) |
| Collector processor pipelines | | Image defaults are enough here | Theory | Not built (by design) |

## Module 7: Backend, infra, Postgres, async

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Connection pool saturation | Pool exhaustion queues every request | Gauges for **both** pools: SQLAlchemy/asyncpg and the psycopg checkpointer | Core | Planned (3) |
| DB query latency | | SQLAlchemy/asyncpg auto-instrumentation spans | Core | Planned (4) |
| USE (CPU) | Utilization/Saturation/Errors of a resource | Voice STT is CPU-bound: real-time factor plus process CPU | Core | Planned (3/5) |
| Event-loop blocking | CPU work stalling every other request | STT duration vs. request latency during voice | Core | Planned (3) |
| Timeouts | | `llm_timeout`, tool timeouts as metric categories | Core | Planned (3) |
| Locks / deadlocks / retry storms | | One write path, no retries | Theory | Not built (by design) |

## Module 8: Errors and SLOs

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Error categories | validation / dependency / programming | `error_category` (already on `agent_events`) as a metric label | Core | Planned (3) |
| SLI / SLO | Measurement / target | 2–3 dashboard panels | Light | Planned (7) |
| Error budget | Allowed failure under the SLO | SLO panels | Light | Planned (7) |
| Burn-rate multi-window alerts, severity tiers | | Overkill for one developer | Theory | Not built (by design) |
| SLA | Contractual commitment | No customers | Theory | Not built (by design) |

## Module 9: Dashboards, cost, security, audit

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Dashboard as code | Reproducible dashboards | `observability/dashboards/*.json` | Core | Planned (5) |
| Metric → trace → logs | The investigation path | Grafana exemplars → Tempo → Loki via `trace_id` | Core | Planned (4/5) |
| PHI redaction / allowlisting | | `redaction.py`, fail-closed allowlist | Core | Planned (1) |
| Secrets out of logs | | Secret-value scrubbing processor | Core | Planned (1) |
| Observability vs audit logs | Sampled, operator-facing vs complete, who/what/before/after | `reschedule_appointment` audit table | Core | Planned (6) |
| Telemetry cost / retention tuning | | n/a locally | Theory | Not built (by design) |

## Module 10: LLM and agent observability

| Concept | What it is | Where / how to see it | Fit | Status |
|---|---|---|---|---|
| Action trace without chain-of-thought | Record decisions, not reasoning | `tracing.py` + `agent_events` + trace panel | Core | Done (pre-existing) |
| Parser hit rate / LLM fallback rate | Share of requests answered without Sonnet | `chat_requests_total{route}` | Core | Planned (3) |
| Jev consultation telemetry | Confidence, tokens, latency, outcome | `jev_invoked` events (pre-existing) + metrics | Core | Partial (events exist) |
| LLM latency / tokens / cost | | LLM duration + token histograms | Core | Planned (3) |
| Tool latency / failure / invalid calls | | `tool_executed`/`tool_requested` events (pre-existing) + metrics | Core | Partial (events exist) |
| Grounding rejection rate | | `error_category="grounding_rejected"` | Core | Partial (events exist) |
| Agent rounds / loop limits / timeouts | | rounds histogram, termination counter | Core | Planned (3) |

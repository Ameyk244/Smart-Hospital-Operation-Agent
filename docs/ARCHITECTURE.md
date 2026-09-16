# Architecture — Smart Hospital Operations Agent

This is a learning project. The goal is not a hospital product — it is a small,
real, traceable system that demonstrates the full engineering path of a modern
agentic backend: deterministic core → agent augmentation → grounded tool calling
→ bounded execution → observability.

All data is synthetic. No real patient information is ever used.

## 1. Guiding principle: one execution path, two entrances

```
User request
     |
Deterministic parser (regex/keyword grammar over a fixed command set)
     |
  Known command? ----NO----> Agent eligibility gate --> LangGraph agent loop
     |YES                                                      |
     v                                                          v
CommandRunner.execute(Command)  <----------------------  execute_command tool
     |                                                    action tools (reschedule_*,
     v                                                     assign_*) also resolve to
Repository layer (SQLAlchemy) -- transaction -- PostgreSQL  a Command + CommandRunner
```

There is exactly **one** canonical implementation per hospital operation
(e.g. "reschedule an appointment"), living in `backend/app/execution/commands/`.
Both the deterministic parser and the agent's tools produce a `Command` object
and hand it to the same `CommandRunner.execute()`. Neither path re-implements
business logic. This is directly testable: `tests/unit/test_command_runner.py`
exercises the runner without any parser or LLM involved.

## 2. Tech stack (decided, not a menu)

| Concern | Choice | Why |
|---|---|---|
| Language/runtime | Python 3.11 (via `py -3.11`) | Best current compatibility with LangChain/LangGraph ecosystem; 3.14 is too new for some pinned deps. |
| Web framework | FastAPI | Native async, Pydantic-based validation, small and explicit. |
| DB | PostgreSQL 16 (Docker Compose) | Required by spec. Local dev via `docker-compose.yml`, no cloud dependency. |
| ORM/migrations | SQLAlchemy 2.0 (async) + Alembic | Explicit repository layer on top; async engine used for real I/O concurrency. |
| Agent framework | LangChain (model/tool/message abstraction) + LangGraph (explicit graph: nodes, typed state, conditional edges) | Required by spec; graph kept small and inspectable, no `create_agent()`-style black box. |
| LLM provider | Provider abstraction (`app/agent/providers/`) with Anthropic as default, OpenRouter as a documented alternative | Keeps the agent from being welded to one vendor; both are OpenAI/Anthropic-tool-calling compatible via LangChain. |
| Schema validation | Pydantic v2 | Tool args/results, API request/response, command payloads. |
| Frontend | React + TypeScript + Vite | Small, fast, no framework ceremony. |
| Structured logging | `structlog` → JSON | Action-level observability (§9 of the spec), never chain-of-thought. |
| Testing | pytest + pytest-asyncio + httpx (API) + a `FakeChatModel` for mocked-LLM tests | Offline suite never calls a live model; live-model tests gated behind `RUN_LIVE_LLM_TESTS=1`. |

No message brokers, no vector DB, no Kubernetes, no microservices. One FastAPI
process, one Postgres instance, one small SPA.

## 3. Data model (9 tables)

- `departments(id, name, code)`
- `staff(id, name, role, department_id)`
- `rooms(id, name, department_id)`
- `scanners(id, name, type, room_id, status)` — type ∈ {MRI, CT, XRAY}
- `patients(id, mrn, name, dob)`
- `appointments(id, patient_id, department_id, scanner_id, staff_id, appointment_type, scheduled_start, scheduled_end, status, notes)`
- `agent_sessions(id, created_at, last_active_at)` — session identity
- `conversation_messages(id, session_id, role, content, created_at)` — short-term conversation memory (persisted so a session survives process restarts)
- `preferences(id, session_id, key, value, created_at)` — persistent, explicit-only memory (remember/forget tools)
- `session_grounded_entities(id, session_id, entity_type, entity_id, exposed_at)` — the grounding ledger (§6 below)
- `agent_events(id, session_id, round_num, event_type, tool_name, arguments_json, status, latency_ms, error_category, created_at)` — the observability trace

LangGraph checkpoint state is **not** a table we design — it's owned by the
LangGraph Postgres checkpointer (`langgraph-checkpoint-postgres`), kept
deliberately separate from `conversation_messages` and `preferences` (§7).

Seeding: `backend/app/seed/seed_data.py`, deterministic (fixed RNG seed),
run via `python -m app.seed.seed_data`. Produces enough overlap (multiple
delayed MRI appointments, multiple compatible scanners) that search tools
return >1 plausible result and grounding actually matters.

## 4. Tool inventory (covers all 7 required categories, nothing extra)

| Tool | Category | Routes through CommandRunner? |
|---|---|---|
| deterministic parser commands (`show next appointment`, `list delayed appointments`, `show patient <name>`, `list scanners`) | 1. deterministic, no LLM | yes (read commands) |
| `execute_command(command_text)` | 2. decomposition → trusted path | yes — parses `command_text` with the *same* parser the deterministic path uses |
| `search_appointments(filters)`, `search_patients(query)`, `search_scanners(filters)` | 3. search/retrieval | no (read-only repository queries; results are what get grounded) |
| `get_entity_details(entity_type, entity_id)` | 4. grounding/context selection | no, but enforces grounding on read |
| `reschedule_appointment(appointment_id, new_scanner_id, new_start_time)`, `assign_scanner(appointment_id, scanner_id)` | 5. action/write | yes — builds a `Command`, same runner as deterministic writes |
| `get_scanner_availability(scanner_id, date)`, `list_department_status(department_id)` | 6. observation/read | no (read-only) |
| `remember_preference(key, value)`, `forget_preference(key)`, `list_preferences()` | 7. memory | no (writes to `preferences`, not hospital data) |

Every write tool's arguments referencing an entity ID are checked against
`session_grounded_entities` **before** a `Command` is constructed. Rejection
happens in code (`app/agent/grounding.py`), not via prompt instruction.

## 5. LangGraph shape

Typed state (`AgentState`, a `TypedDict`): `session_id`, `messages`,
`round_count`, `tool_call_count`, `invalid_call_count`, `terminated_reason`.

Nodes:
- `agent_node` — binds the tool registry to the chat model, sends `messages`, appends the LLM's response.
- `tool_node` — for each requested tool call: validate args (Pydantic) → grounding check → execute → log an `agent_events` row → append a `ToolMessage`.
- conditional edge after `agent_node`: tool calls present → `tool_node`; else → `END`.
- edge after `tool_node`: bounds exceeded → `END` (with `terminated_reason` set); else → back to `agent_node`.

Bounded execution (§8 of spec), all configurable via `app/config.py`:
`MAX_AGENT_ROUNDS` (default 6), `MAX_TOOL_CALLS` (default 10),
`TOOL_TIMEOUT_SECONDS` (default 15, enforced with `asyncio.wait_for`),
`MAX_INVALID_TOOL_CALLS` (default 3, consecutive).

## 6. Grounding-by-rejection

`app/agent/grounding.py`: `GroundingRegistry.expose(session_id, entity_type, ids)`
called by every search/read tool; `GroundingRegistry.check(session_id,
entity_type, id)` called by every write tool before touching the DB. Backed by
the `session_grounded_entities` table so it survives process restarts and is
directly queryable/testable. Adversarial tests feed fabricated IDs
(`APT-999`) straight into the write tools (bypassing the LLM) to prove
rejection happens at the code boundary.

## 7. Three separate memories (never merged)

1. **Conversation/session state** — `conversation_messages` + `AgentState.messages` for the in-flight run. Keyed by `session_id`.
2. **Persistent preferences** — `preferences` table, only touched by the explicit `remember_preference`/`forget_preference` tools, injected into the agent's system context at graph start (not mid-conversation) and only for that session.
3. **LangGraph checkpoint** — `langgraph-checkpoint-postgres`, purely execution/resumption state for the graph itself, never read or written by application code directly.

## 8. Observability

Every tool call and every routing decision emits one `agent_events` row and
one structured log line: session, round, tool name, args (redacted if
sensitive — none are, since data is synthetic), validation outcome, latency,
success/failure, error category. A `GET /api/sessions/{id}/trace` endpoint
serves this for the UI's trace panel (SSE for live updates during a request).
No chain-of-thought/private reasoning is ever captured — only observable
actions and their outcomes.

## 9. Phase plan (dependency-ordered, one or more commits per phase)

0. Repo scaffold, `.gitignore`, `.env.example`, `docker-compose.yml`, this doc. **(this commit)**
1. DB schema (SQLAlchemy models + Alembic migration) + repository layer.
2. Synthetic seed data generator + seeding tests.
3. Deterministic parser + `Command` grammar + `CommandRunner` + execution functions, fully tested without any LLM/agent code.
4. FastAPI skeleton wired to the deterministic path only (`POST /api/commands`). **← thin vertical slice ends here if no LLM key yet**
5. LLM provider abstraction (Anthropic + OpenRouter adapters) — implemented, but live calls gated behind the secret checkpoint (§15).
6. First real structured tool (`search_appointments`) + tool registry + grounding registry.
7. LangGraph loop: `agent_node` + `tool_node` + conditional routing + typed state + bounded execution. **← full vertical slice, needs live key to exercise end-to-end**
8. Remaining tools (action/write, observation, memory) + `execute_command` decomposition tool.
9. Session/conversation persistence + preference memory + LangGraph Postgres checkpointer.
10. Observability: structured logging + `agent_events` + trace endpoint.
11. Frontend: operations view + chat panel + trace panel.
12. Full test layers: unit, mocked-LLM agent-loop, adversarial/grounding, integration (real Postgres), gated live-model, E2E.
13. `docs/CONCEPT_COVERAGE.md` completed and verified against the running system; final audit.

Each phase = its own commit(s); tests for that phase run and pass first.
`docs/PROGRESS.md` is updated as phases complete, not reconstructed later.

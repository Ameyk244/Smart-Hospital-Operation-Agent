# Architecture - Smart Hospital Operations Agent

This is a learning project built over synthetic hospital data. Its core design
is deterministic-first: exact commands avoid model calls, near-miss read
requests can use a typed Jev routing decision, and richer hospital language
falls through to a bounded, grounded LangGraph agent.

## 1. Current Request Flow

```text
POST /api/chat
      |
      v
Create/reuse session, load recent messages, persist the user message
      |
      v
parse(request.text)                       app/parser/parser.py
      |
   matched?
   /      \
 YES       NO
  |         |
  v         v
CommandRunner    check_domain_gate(text, recent user messages)
.execute()                    |
  |                       in domain?
  |                        /      \
  |                      NO        YES
  |                      |          |
  |                      v          v
  |                   rejected  check_eligibility(text)
  |                                  |
  |                              eligible?
  |                               /      \
  |                             NO        YES
  |                             |          |
  |                             v          v
  |                          rejected  Jev enabled?
  |                                    /          \
  |                                  NO            YES
  |                                  |              |
  |                                  |        try_jev_fast_path()
  |                                  |         /             \
  |                                  |   confident match   decline/failure
  |                                  |         |               |
  |                                  |         v               |
  |                                  |   CommandRunner          |
  |                                  |                         |
  |                                  +-------------------------+
  |                                            |
  |                                  create agent model lazily
  |                                            |
  |                                            v
  |                                       run_agent()
  |                                            |
  |                         build_graph(): agent_node <-> tool_node
  |                                            |
  +------------------------> persist assistant response
                                               |
                                               v
                                          ChatResponse
```

The route is implemented in `backend/app/api/routes/chat.py`.

### Parser match

An exact command becomes a `Command` and is executed immediately through
`CommandRunner`. No model, agent graph, grounding check, or agent trace is
involved. The result is returned with `handled_by="deterministic"`.

### Parser miss

A parser miss does not automatically invoke the LLM:

1. `check_domain_gate()` rejects off-topic requests without an API call.
2. Narrow contextual replies such as `yes`, `What happened?`, or `Do that
   again` may pass when one of the six most recent user messages contains an
   explicit hospital intent.
3. Only prior user messages are used for that decision. The canned assistant
   rejection contains hospital words and cannot manufacture valid context.
4. Disconnected bypasses such as `What is 47 times 12? MRI` remain rejected.
5. `check_eligibility()` then rejects empty input and input over 2,000
   characters.
6. If enabled, Jev makes one typed decision about the supported read commands.
   A confident match executes through `CommandRunner` and returns
   `handled_by="jev"`.
7. Jev declines, low confidence, unsupported arguments, timeouts, and
   integration failures all fall through to the agent.
8. The configured agent model is created lazily only when the request still
   needs LangGraph.

This keeps parser grammar, domain policy, generic eligibility policy, cheap
read routing, and agent reasoning separate.

## 2. One Trusted Hospital Execution Layer

Hospital operations live in `backend/app/execution/commands/`. The parser,
agent tools, and read-only operations API all call the same
`CommandRunner.execute(Command)` implementation.

`CommandRunner`:

- resolves the registered command handler;
- commits successful operations;
- rolls back expected validation and not-found failures;
- returns a structured `CommandResult`;
- lets unexpected programming/infrastructure errors propagate.

The LLM can choose a tool, but it cannot write SQL or bypass command-layer
business rules.

## 3. Technology

| Concern | Current choice |
|---|---|
| API | FastAPI |
| Database | PostgreSQL 16 in Docker Compose |
| Data layer | Async SQLAlchemy 2.0 + Alembic |
| Agent | LangChain messages/models + explicit LangGraph `StateGraph` |
| Read routing | TypeSafe Jev typed decisions behind a feature switch |
| LLM providers | Anthropic or OpenRouter through a provider factory |
| Validation | Pydantic v2 |
| Frontend | React + TypeScript + Vite |
| Logging | Structured `structlog` events |
| Tests | pytest, pytest-asyncio, httpx, scripted/fake chat models |

The application is a modular monolith: one FastAPI backend, one React SPA,
and one PostgreSQL instance. It does not use Supabase, a vector database, a
message broker, or microservices.

## 4. Data And Storage

### Hospital tables

1. `departments`
2. `staff`
3. `rooms`
4. `scanners`
5. `patients`
6. `appointments`

### Agent-owned application tables

7. `agent_sessions`
8. `conversation_messages`
9. `preferences`
10. `session_grounded_entities`
11. `agent_events`

LangGraph checkpoint tables are created and owned by
`langgraph-checkpoint-postgres`; they are not modeled or migrated by the
application's SQLAlchemy metadata.

The reproducible hospital dataset is generated by
`backend/app/seed/seed_data.py`. A full reseed replaces hospital-domain rows
but does not intentionally reset agent conversation, preference, grounding,
or event lifecycles.

## 5. Deterministic Parser And Commands

The parser is intentionally a small regex grammar, not a natural-language
understanding layer. All parser-exposed commands are read-only:

| Input shape | Canonical command |
|---|---|
| `list departments` | `list_departments` |
| `list scanners [type] [status]` | `list_scanners` |
| `show patient <name>` | `search_patients` |
| `show [the] next appointment` | `show_next_appointment` |
| `list delayed appointments [type]` | `list_delayed_appointments` |

The command registry also contains operations used by APIs or agent tools:

| Command | Access |
|---|---|
| `get_scanner_availability` | READ |
| `list_patient_appointments` | READ |
| `search_appointments` | READ |
| `reassign_scanner` | WRITE |

`reassign_scanner` is the only canonical hospital write. It validates that
the appointment and scanner exist, scanner modality matches the appointment,
and the scanner is currently `AVAILABLE`. It preserves duration and may keep
the current start time or use a supplied ISO-8601 start time.

## 6. Registered Agent Tools

There are exactly seven registered agent tools:

| Tool | Access | CommandRunner | Purpose |
|---|---|---|---|
| `search_appointments` | READ | Yes | Filter appointments and ground returned appointment, scanner, and patient codes |
| `execute_command` | READ | Yes | Parse and run one exact deterministic command; ground codes in its result |
| `get_scanner_availability` | READ | Yes | Read one already-grounded scanner's status |
| `reschedule_appointment` | HOSPITAL WRITE | Yes | Reassign a grounded appointment to a grounded scanner and optionally change time |
| `list_preferences` | READ | No | Read explicit session preferences |
| `remember_preference` | MEMORY WRITE | No | Store an explicitly requested preference |
| `forget_preference` | MEMORY WRITE | No | Remove a preference by key |

The agent cannot create/delete appointments, create/edit patients, change
scanner status, edit departments, run arbitrary SQL, or invoke an unregistered
operation. `reschedule_appointment` is its only hospital-data write.

## 7. LangGraph Execution

`run_agent()` compiles a small graph for each eligible agent request using the
long-lived PostgreSQL checkpointer created during FastAPI startup.

```text
agent_node
   | no tool calls / termination
   +-----------------------------> END
   |
   | tool calls
   v
tool_node
   | termination
   +-----------------------------> END
   |
   +-----------------------------> agent_node
```

`agent_node` binds the seven tool schemas, records invocation/response events,
calls the model under an LLM timeout, and appends its response.

`tool_node` handles every requested tool call in a fresh database session:

1. enforce the per-turn tool-call bound;
2. reject unknown tools;
3. validate arguments with the tool's Pydantic schema;
4. execute the handler under a timeout;
5. catch grounding, expected tool, validation, and timeout failures;
6. commit successful results or failure events;
7. return a `ToolMessage` to the model;
8. collect entity codes touched during this turn.

The typed `AgentState` contains session ID, messages, round/tool/invalid-call
counters, termination reason, and touched entity codes. Messages use
LangGraph's `add_messages` reducer; counters and touched codes reset for every
new user turn.

## 8. Grounding By Rejection

Grounding is a code-level authorization rule for entity references:

1. `search_appointments` or `execute_command` returns real entities.
2. Those tools expose typed codes in `session_grounded_entities`.
3. `get_scanner_availability` requires a grounded scanner code.
4. `reschedule_appointment` requires both a grounded appointment code and a
   grounded scanner code.
5. Command-layer validation still checks existence, modality, and availability.

Grounding is scoped by `session_id` and survives process restarts. A direct
deterministic chat command does not ground codes because the LLM did not choose
them; the agent's `execute_command` tool does ground its returned codes.

## 9. Conversation, Preferences, And Checkpoints

These are intentionally separate:

1. `conversation_messages` is the durable human-readable transcript used by
   the API and as fallback history when no checkpointer is configured.
2. `preferences` stores only values explicitly written through the memory
   tools. Preferences are loaded into the system message for an agent turn.
3. LangGraph checkpoint tables store graph/thread state. `thread_id` equals
   the application `session_id`.

With the production checkpointer active, prior graph messages come from the
checkpoint to avoid duplicating `conversation_messages`. Without a
checkpointer, the route supplies converted recent conversation history.

## 10. Scope And Safety

The system prompt restricts the agent to synthetic hospital scheduling and
scanner assignment. It instructs the model to search before acting, use only
grounded codes, use preference tools only on explicit request, decline
non-hospital subtasks, and keep answers concise.

Safety does not rely on the prompt alone. The domain gate, Pydantic argument
validation, grounding registry, command business rules, transaction handling,
and bounded graph execution enforce the important constraints in code.

Default per-turn bounds:

| Setting | Default |
|---|---:|
| `MAX_AGENT_ROUNDS` | 6 |
| `MAX_TOOL_CALLS` | 10 |
| `MAX_INVALID_TOOL_CALLS` | 3 |
| `TOOL_TIMEOUT_SECONDS` | 15 |
| `LLM_TIMEOUT_SECONDS` | 30 |

LangGraph also receives a derived recursion limit of
`MAX_AGENT_ROUNDS * 4 + 10`.

## 11. Observability

The agent records action-level events, never private chain-of-thought. Events
include model invocation/response, requested tools, validation failures,
grounding rejection, tool success/failure/timeout, and bound termination.

Events are written to `agent_events` and structured logs. The frontend reads
them through `GET /api/sessions/{session_id}/trace`. The transport is polling,
including short-interval polling during an active turn; it is not SSE or a
WebSocket.

Jev consultations record `jev_invoked` with the selected command, confidence,
token counts, latency, and outcome. Deterministic and pre-model rejection paths
persist conversation messages but do not create LangGraph tool traces.

## 12. API Surface

| Endpoint | Purpose |
|---|---|
| `POST /api/chat` | Unified deterministic/Jev/rejected/agent request path |
| `GET /api/cost-comparison` | Aggregate measured Jev use and estimated avoided agent cost |
| `GET /api/operations/departments` | Read departments through `CommandRunner` |
| `GET /api/operations/scanners` | Read/filter scanners through `CommandRunner` |
| `GET /api/operations/appointments` | Read/filter appointments through `CommandRunner` |
| `GET /api/operations/patients` | Search patients through `CommandRunner` |
| `GET /api/sessions/{id}/messages` | Restore persisted conversation messages |
| `GET /api/sessions/{id}/trace` | Read agent action events |
| `GET /api/health` | Health check |

## 13. Verification Strategy

Normal verification is offline and does not consume model API quota:

- parser/domain-gate/eligibility unit tests;
- command and repository integration tests against PostgreSQL;
- scripted/fake-model LangGraph tests;
- grounding and adversarial tests;
- mocked HTTP tests for routing and contextual follow-ups;
- fake-SDK Jev decision, fallback, gate-order, and cost endpoint tests;
- frontend unit/build/lint checks.

Live-provider tests are explicitly marked `live_llm` and run only when
`RUN_LIVE_LLM_TESTS=1` and credentials are available.

# Smart Hospital Operations Agent Guide

This document explains the runtime hospital agent: why it exists, when it is
activated, what it may read or write, how its tools work, and which code-level
controls keep it bounded. For the complete application structure, see
`docs/ARCHITECTURE.md`. For copy-paste commands and manual checks, see
`rulebook.md`.

## 1. Why The Agent Exists

The deterministic parser is intentionally strict. It is excellent for exact,
known commands such as `list scanners mri available`, but it should not grow
into a second natural-language system.

The agent is the fallback language layer. It handles requests such as:

```text
Which delayed MRI appointments could be moved to an available scanner?
Move the first one you found and keep the same time.
What did you just change?
```

The LLM interprets the request and selects tools. It does not own hospital
business logic, execute SQL, or receive unrestricted database access.

## 2. Activation Rules

Every `POST /api/chat` request follows this order:

1. Create or reuse a session.
2. Load recent conversation messages.
3. Persist the new user message.
4. Try the deterministic parser.
5. If the parser matches, execute the command and stop. The agent is never
   constructed.
6. If the parser misses, run the context-aware domain gate.
7. If the request is in-domain, run the eligibility gate.
8. If enabled, ask Jev whether the request confidently maps to a supported
   read command. A match runs through `CommandRunner` and stops.
9. If Jev declines, lacks confidence, is unavailable, or cannot represent the
   request, create the configured agent model and start LangGraph.

The response identifies the selected route:

| `handled_by` | Meaning |
|---|---|
| `deterministic` | Exact parser command; no LLM call |
| `jev` | Confident typed decision routed to a read command |
| `agent` | LangGraph/model path ran |
| `rejected` | Domain or eligibility policy stopped the request before the model |

Jev is a routing fast path, not an eighth agent tool. It runs before
LangGraph, cannot perform a hospital write, and never bypasses the domain,
eligibility, grounding, or command-validation layers. Its full contract is in
`JEV.md`.

## 3. Domain Gate

The agent's domain is synthetic hospital operations, specifically:

- departments and hospital staff roles;
- patients and appointment lookup;
- appointment status and modality;
- scanners, scanner modality, and scanner availability;
- appointment scheduling and scanner reassignment;
- explicit session preferences.

The domain gate is synchronous regex/set logic, not another model call. A
normal request must contain a connected hospital noun and action/question
shape. This rejects weather, arithmetic, creative writing, travel booking,
and other unrelated work before API tokens are spent.

Narrow follow-ups may pass without repeating a hospital noun when recent user
history establishes the context. Examples include `yes`, `What happened?`,
`Why?`, `Do that again`, and `the first one`. The gate checks only the six most
recent prior user messages for explicit hospital intent. It does not use
assistant rejection text as context, and it does not reopen disconnected
keyword bypasses such as `What is 47 times 12? MRI`.

## 4. Eligibility Gate

After domain relevance passes, `check_eligibility()` applies generic request
policy:

- blank input is rejected as `empty_request`;
- input over 2,000 characters is rejected as `request_too_long`.

Domain relevance and eligibility are separate so future policies such as
authentication, rate limits, or blocklists can be added without changing the
parser or graph.

## 5. Deterministic Parser: Read-Only Entrance

All parser-exposed commands are reads:

| Exact shape | Canonical operation | Access |
|---|---|---|
| `list departments` | `list_departments` | READ |
| `list scanners [mri|ct|xray] [available|in use|maintenance]` | `list_scanners` | READ |
| `show patient <name>` | `search_patients` | READ |
| `show [the] next appointment` | `show_next_appointment` | READ |
| `list delayed appointments [mri|ct|xray]` | `list_delayed_appointments` | READ |

Matching is case-insensitive, collapses repeated whitespace and ignores
trailing punctuation (spoken transcripts and typed sentences routinely end in
`.` or `?`), but is not fuzzy. A phrase such as `show me the next appointment` deliberately misses
the grammar and becomes an agent candidate.

Direct deterministic commands do not create grounding records. Grounding is
an LLM safety boundary and is created by agent tools that expose entities.

## 6. Canonical Hospital Operations

Both entrances converge on `CommandRunner`. Registered operations are:

| Operation | Access | Used by |
|---|---|---|
| `list_departments` | READ | Parser, `execute_command`, operations API |
| `list_scanners` | READ | Parser, `execute_command`, operations API |
| `get_scanner_availability` | READ | Agent observation tool |
| `search_patients` | READ | Parser, `execute_command`, operations API |
| `list_patient_appointments` | READ | Internal command surface |
| `show_next_appointment` | READ | Parser and `execute_command` |
| `search_appointments` | READ | Agent search tool and operations API |
| `list_delayed_appointments` | READ | Parser and `execute_command` |
| `reassign_scanner` | WRITE | Agent `reschedule_appointment` tool |

`CommandRunner` commits success and rolls back expected command/not-found
failures. Unexpected infrastructure or programming errors are not hidden.

## 7. Agent Tool Inventory

The model sees exactly seven tool schemas.

### Hospital reads

#### `search_appointments`

Filters by status, modality, patient code, department code, scanner code, and
limit. It executes the canonical `search_appointments` command and grounds
every appointment, scanner, and patient code returned.

#### `execute_command`

Accepts an exact parser command string and routes it through the same parser
and `CommandRunner`. It cannot write because the parser grammar exposes only
read commands. It grounds typed entity codes found in the result.

#### `get_scanner_availability`

Returns one scanner's `AVAILABLE`, `IN_USE`, or `MAINTENANCE` status through
the canonical command layer. The scanner must already be grounded.

### Hospital write

#### `reschedule_appointment`

This is the agent's only hospital-data write. It requires a grounded
appointment code and scanner code, then executes `reassign_scanner`.

The command layer enforces:

- appointment exists;
- target scanner exists;
- scanner modality matches the appointment type;
- scanner status is `AVAILABLE`;
- supplied start time is valid ISO-8601;
- appointment duration is preserved, with a 45-minute fallback.

The operation can change scanner and optionally start/end time. A successful
reassignment sets the appointment status to `SCHEDULED`.

### Session-memory tools

#### `remember_preference`

Stores a key/value preference only when the user explicitly requests it.

#### `forget_preference`

Deletes an explicitly named preference key.

#### `list_preferences`

Lists preferences for the active session.

Preference writes are not hospital writes and do not use `CommandRunner`.

## 8. What The Agent Cannot Do

No registered tool permits the agent to:

- create or delete patients;
- create, delete, or cancel appointments;
- edit patient demographics;
- change scanner status;
- create or edit departments, rooms, or staff;
- run arbitrary SQL;
- invoke Alembic or change the schema;
- use fabricated or cross-session entity codes;
- answer unrelated general-knowledge subtasks through a hospital request.

The model may describe a limitation, but it cannot manufacture a capability
that is absent from the tool registry.

## 9. Grounding

Grounding answers: "Was this typed entity code exposed by a trusted tool in
this session?"

```text
search/read tool returns code
            |
            v
session_grounded_entities
            |
            v
specific read/write requires matching session + entity type + code
```

Important properties:

- grounding is persisted in PostgreSQL;
- grounding is scoped to `session_id`;
- entity type matters: a department code cannot satisfy a scanner check;
- fabricated but well-formed codes are rejected;
- codes seen in another chat session are rejected;
- grounding does not replace business validation.

`search_appointments` and `execute_command` expose codes. The scanner
observation and appointment write tools call `require_grounded()` before the
canonical operation runs. Grounding failures become rejected tool results for
the model to explain; they do not reach the write command.

## 10. LangGraph Loop

The graph has two nodes:

```text
agent_node -- tool calls --> tool_node -- continue --> agent_node
    |                         |
    | final answer            | bound reached
    v                         v
   END                       END
```

### `agent_node`

- binds the seven registered tools to the provider-neutral chat model;
- records `agent_invoked`;
- calls the model under `LLM_TIMEOUT_SECONDS`;
- records `llm_response` or `llm_timeout`;
- routes to tools only when the response contains tool calls.

### `tool_node`

For each requested call, it:

1. increments/enforces the tool-call limit;
2. rejects unknown tool names;
3. validates arguments with Pydantic;
4. opens a fresh async database session;
5. runs the handler under `TOOL_TIMEOUT_SECONDS`;
6. catches grounding, validation, expected tool, and timeout errors;
7. records an action event;
8. returns JSON through a `ToolMessage`;
9. commits successful tool effects;
10. accumulates entity codes touched for frontend highlighting.

After a tool result, the model runs again to interpret the result and produce
the user-facing answer. This is why one visible tool call commonly involves
two paid model calls.

## 11. Agent State And Bounds

`AgentState` contains:

- `session_id`;
- LangChain `messages` with the `add_messages` reducer;
- `round_count`;
- `tool_call_count`;
- `invalid_call_count`;
- `terminated_reason`;
- `touched_entity_codes` for the current turn.

Default limits:

| Limit | Default termination behavior |
|---|---|
| 6 model rounds | `max_rounds_exceeded` |
| 10 tool calls | `max_tool_calls_exceeded` |
| 3 invalid calls | `too_many_invalid_calls` |
| 15-second tool timeout | Failed tool message |
| 30-second LLM timeout | `llm_timeout` |

The graph recursion limit is derived as `max_agent_rounds * 4 + 10`.

## 12. Prompt And Provider

The system prompt restricts the model to scheduling and scanner assignment
over synthetic data. It instructs the model to search before acting, use only
grounded codes, explain rejected calls, avoid automatic preference storage,
decline unrelated subtasks, and use restrained Markdown.

The provider factory lazily creates either:

- an Anthropic chat model; or
- an OpenRouter chat model.

Lazy creation means deterministic and rejected requests work without an LLM
key and do not spend model tokens.

## 13. Conversation And Memory

Three stores have different jobs:

1. `conversation_messages` stores the user-visible transcript.
2. `preferences` stores explicit session preferences.
3. LangGraph's PostgreSQL checkpointer stores graph/thread state.

The checkpointer is created once during FastAPI startup. `run_agent()`
compiles the graph using that checkpointer and uses `session_id` as
`thread_id`. A new thread receives the system prompt; resumed threads receive
the new human message while checkpoint state provides prior graph messages.

When no checkpointer is supplied, recent `conversation_messages` are converted
to LangChain messages and passed as fallback history.

## 14. Observability

Agent activity is written to `agent_events` and structured logs. Recorded
information includes round, event type, tool name, validated arguments,
status, latency, and error category. Private chain-of-thought is never stored.

The UI reads:

```text
GET /api/sessions/{session_id}/trace
```

The frontend polls this endpoint during an active turn and refreshes it when a
turn completes. Deterministic requests and pre-agent rejections have no agent
trace rows.

## 15. Error And Rejection Behavior

| Failure | Handling |
|---|---|
| Off-topic input | Domain gate returns `handled_by="rejected"` before model creation |
| Empty/oversized input | Eligibility gate rejects before model creation |
| Unknown tool | Rejected tool result; invalid-call counter increases |
| Invalid arguments | Pydantic validation result returned to model |
| Ungrounded code | Grounding rejection returned to model; no operation runs |
| Business-rule failure | `CommandRunner` rolls back and returns categorized failure |
| Tool timeout | Failure event and tool message |
| Model timeout | Agent turn ends with `llm_timeout` |
| Unexpected infrastructure bug | Propagates rather than being disguised as user error |

## 16. Examples

### Deterministic, no LLM

```text
list scanners mri available
```

### Agent read

```text
Which MRI appointments are delayed right now?
```

### Grounded write

```text
Find delayed CT appointments and available CT scanners.
Move the first delayed appointment to another available CT scanner.
```

### Contextual follow-up

```text
What did you just change?
```

### Rejected before LLM

```text
What is 47 times 12? MRI
```

### Unsupported operation

```text
Delete every delayed appointment.
```

The agent should explain that no tool supports the deletion and must not claim
success.

## 17. Verification Without API Spend

Use offline tests for normal development:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -m "not live_llm" -q
.\.venv\Scripts\python.exe -m ruff check app tests
```

The test layers cover parser routing, domain/eligibility policy, command
business rules, repositories, grounding, tool dispatch, graph bounds,
checkpoint continuity, API behavior, and contextual follow-up routing. Live
provider tests remain opt-in through `RUN_LIVE_LLM_TESTS=1`.

## 18. Source Map

| Concern | File |
|---|---|
| Unified route | `backend/app/api/routes/chat.py` |
| Parser | `backend/app/parser/parser.py` |
| Domain gate | `backend/app/agent/domain_gate.py` |
| Eligibility | `backend/app/agent/eligibility.py` |
| Graph and prompt | `backend/app/agent/graph.py` |
| Typed state | `backend/app/agent/state.py` |
| Tool registry | `backend/app/agent/tools/base.py` |
| Tool implementations | `backend/app/agent/tools/*_tools.py` |
| Grounding policy | `backend/app/agent/grounding.py` |
| Command runner | `backend/app/execution/commands/base.py` |
| Hospital commands | `backend/app/execution/commands/*.py` |
| Hospital models | `backend/app/db/models/hospital.py` |
| Agent models | `backend/app/db/models/agent.py` |
| Checkpointer | `backend/app/agent/checkpointer.py` |
| Trace writer | `backend/app/observability/tracing.py` |
| Settings | `backend/app/config.py` |

# Concept Coverage Audit

Maps each of the 58 required concepts (master prompt §18) to: why it exists
in this system, where it's implemented, how it works, what tests cover it,
and current status. Updated as each phase lands — not reconstructed at the
end. Status values: **Done** (implemented + tested + exercised by the
running app), **Partial** (implemented but not fully exercised/tested yet),
**Not started**.

## LLM + tool calling

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 1 | Real LLM inference | The agent's actual reasoning/tool-selection engine | `app/agent/providers/*` | live-model suite (gated) | Not started |
| 2 | LangChain model abstraction | Vendor-neutral chat model + message + tool-calling interface | `app/agent/providers/*` | — | Not started |
| 3 | System prompting | Establishes the agent's role, tool set, and constraints | `app/agent/graph.py` | mocked-LLM agent-loop tests | Not started |
| 4 | Structured tool/function calling | Real typed tool calls, never string-parsed pseudo-tools | `app/agent/tools/*` | mocked-LLM tests, adversarial tests | Not started |
| 5 | Model-driven tool selection | The LLM decides which tool(s) to call | `app/agent/graph.py` (agent_node) | agent-loop tests | Not started |
| 6 | Multiple tools | Search, grounding, action, observation, memory, decomposition | `app/agent/tools/*` | per-tool unit/integration tests | Not started |
| 7 | Tool registry | One place tools are declared and bound to the model | `app/agent/tools/registry.py` | — | Not started |
| 8 | Tool execution layer | Validates args, checks grounding, executes, times out | `app/agent/graph.py` (tool_node) | agent-loop, timeout, adversarial tests | Not started |
| 9 | Tool results returned to the LLM | `ToolMessage` round-trip in the graph | `app/agent/graph.py` | agent-loop tests | Not started |
| 10 | Multi-tool agent loop | Several tool calls across multiple rounds before finishing | `app/agent/graph.py` | agent-loop tests | Not started |

## LangGraph + orchestration

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 11 | LangGraph orchestration | Explicit, inspectable graph instead of a black-box agent call | `app/agent/graph.py` | agent-loop tests | Not started |
| 12 | Conditional graph routing | tool-calls-present vs. finished vs. bounds-exceeded | `app/agent/graph.py` | agent-loop, bounded-execution tests | Not started |
| 13 | Typed graph state | `AgentState` TypedDict, not a loose dict | `app/agent/state.py` | agent-loop tests | Not started |

## Deterministic + agent architecture

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 14 | Deterministic fast path | Known requests never touch the LLM | `app/parser/parser.py` | `tests/unit/test_parser.py` | **Done** |
| 15 | LLM fallback | Unknown requests become eligible for the agent | `app/agent/eligibility.py` | eligibility-gate tests | Not started |
| 16 | Command parser | Regex/keyword grammar over a fixed command set | `app/parser/parser.py` | `tests/unit/test_parser.py` | **Done** |
| 17 | Structured intents | `Command(name, args)`, not a free-text string | `app/execution/commands/base.py` | parser + command-runner tests | **Done** |
| 18 | Command runner | Single execution chokepoint for both entrances | `app/execution/commands/base.py` (`CommandRunner`) | `tests/integration/test_command_runner.py` | **Done** |
| 19 | Agent → existing execution path | Agent write tools resolve to the same `Command`s | `app/agent/tools/action_tools.py`, `command_tools.py` | agent-loop tests (once tools exist) | Not started (runner side is done; agent side pending) |
| 20 | Natural-language decomposition | `execute_command` tool re-parses an LLM-identified sub-instruction through the same parser | `app/agent/tools/command_tools.py` | agent-loop tests | Not started |
| 21 | Shared command grammar | One `Command` vocabulary used by parser and agent tools alike | `app/execution/commands/*` | `tests/integration/test_parser_to_runner.py` | **Done** (grammar side); agent reuse pending |

## Grounding + safety

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 22 | Grounding | Track which entity codes were actually exposed to a session | `app/db/models/agent.py` (`SessionGroundedEntity`), `app/db/repositories/grounding_repository.py` | `tests/integration/test_repositories.py::test_grounding_expose_and_check` | Partial (ledger + repo done; policy layer `app/agent/grounding.py` not yet written) |
| 23 | Grounding-by-rejection | A fabricated ID must be rejected in code, not by prompting | `app/agent/grounding.py` (pending) | adversarial tests (pending) | Not started |
| 24 | Argument validation | Tool args are Pydantic-validated before execution | `app/agent/tools/*` schemas | adversarial tests | Not started |
| 25 | Read vs. action/write tools | Different risk profiles, different validation depth | `app/agent/tools/{search,observation}_tools.py` vs `action_tools.py` | tool tests | Not started |
| 26 | Error-aware tools | Tool failures return structured errors the model can react to | `app/execution/commands/base.py` (`CommandError`/`CommandResult`) | `tests/integration/test_command_runner.py` (modality/availability rejections) | **Done** at the command layer; tool-layer wiring pending |

## Bounded agent execution

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 27 | Max rounds/tool-call limits | Prevents runaway loops | `app/config.py` (`max_agent_rounds`, `max_tool_calls`), enforced in `app/agent/graph.py` | bounded-execution tests | Config exists; enforcement not started |
| 28 | Timeouts | Model/tool/DB calls can't hang forever | `app/config.py` (`tool_timeout_seconds`), `app/agent/graph.py` | timeout tests | Config exists; enforcement not started |
| 29 | Invalid-call termination | Repeated bad tool calls end the session gracefully | `app/config.py` (`max_invalid_tool_calls`), `app/agent/graph.py` | adversarial tests | Config exists; enforcement not started |

## Async backend

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 30 | Python async programming | Real I/O concurrency for DB/LLM/tool calls, not decorative | `app/db/session.py` (async engine), all repositories, FastAPI routes | full test suite (all async) | **Done** for the DB layer; LLM-call concurrency pending |

## PostgreSQL + application data

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 31 | PostgreSQL | Primary datastore, real transactions | `docker-compose.yml`, `app/db/*` | full integration suite | **Done** |
| 32 | Repository/data-access layer | No SQL scattered through agent/tool code | `app/db/repositories/*` | `tests/integration/test_repositories.py` | **Done** |
| 33 | Transactions | Mutating commands commit-or-rollback atomically | `app/execution/commands/base.py` (`CommandRunner.execute`) | `tests/integration/test_command_runner.py::test_reassign_scanner_is_transactional_on_failure` | **Done** |

## State + memory

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 34 | Conversation/session identity | Every session has a stable id | `app/db/models/agent.py` (`AgentSession`) | `tests/integration/test_repositories.py` | **Done** (model + repo); not yet wired to a live chat endpoint |
| 35 | Short-term conversation state | Durable transcript per session | `ConversationMessage`, `SessionRepository` | `test_conversation_messages_ordered_oldest_first` | **Done** (storage layer); not yet read by an agent |
| 36 | LangGraph checkpointing | Graph execution/resumption state, kept separate from conversation state | `langgraph-checkpoint-postgres` (dependency installed) | — | Not started |
| 37 | Persistent preference memory | Explicit, session-scoped, never inferred | `Preference`, `PreferenceRepository` | `test_preference_remember_forget_roundtrip` | **Done** (storage layer); tools pending |
| 38 | Explicit memory tools | `remember_preference`/`forget_preference`/`list_preferences` | `app/agent/tools/memory_tools.py` (pending) | — | Not started |
| 39 | Memory separation | Conversation / preferences / checkpoint never merged | `app/db/models/agent.py` module docstring + separate tables | — | **Done** (schema-level); needs to hold up once the agent reads all three |
| 40 | Memory injection | Preferences injected into agent context only when relevant | `app/agent/graph.py` (pending) | — | Not started |
| 41 | Memory safeguards | Preferences can't be silently overwritten by inference | `PreferenceRepository.remember` is upsert-by-explicit-call only | — | **Done** at the repository boundary |

## Application context

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 42 | Current application context | "the next appointment", "that scanner" resolved from recent state | `app/agent/state.py` (pending) | — | Not started |
| 43 | Context updates after tools | Grounded set / selected entity updates as tools run | `app/agent/graph.py` tool_node (pending) | — | Not started |

## Agent activation

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 44 | Agent eligibility gate | Decides whether an unmatched request is even allowed to reach the LLM | `app/agent/eligibility.py` (pending) | — | Not started |

## Configuration + providers

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 45 | Configuration layer | One typed source of truth for every setting | `app/config.py` | implicit (used throughout) | **Done** |
| 46 | Secrets management | No secrets committed; `.env` + `.env.example` | `backend/.env.example`, `.gitignore` | manual verification | **Done** |
| 47 | Provider abstraction | Agent not welded to one LLM vendor | `app/agent/providers/*` (pending) | — | Not started |

## Observability

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 48 | Observability | Action-level execution trace | `app/db/models/agent.py` (`AgentEvent`), `EventRepository` | `tests/integration/test_repositories.py` (repo only so far) | Partial |
| 49 | Structured logging | JSON logs for every action-level decision | `app/observability/*` (pending, `structlog` installed) | — | Not started |
| 50 | Agent traceability (no hidden chain-of-thought) | Only observable actions are ever logged/stored | `AgentEvent` schema itself has no "reasoning" field, by design | — | **Done** (schema-level guarantee); needs the emitting code |

## Testing

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 51 | Unit tests | Fast, no-DB, no-LLM tests | `tests/unit/test_parser.py` | — | **Done** (parser); more to come |
| 52 | Mocked LLM tests | Deterministic agent-loop tests without live API calls | `app/agent/providers/fake.py` + `tests/unit/test_agent_loop.py` (pending) | — | Not started |
| 53 | Real tool integration tests | Tools actually hit the real DB | `tests/integration/test_command_runner.py` | — | **Done** (command layer); tool-wrapper layer pending |
| 54 | Parser → executor tests | Deterministic path, no LLM | `tests/integration/test_parser_to_runner.py` | — | **Done** |
| 55 | Agent-loop tests | Multiple tool rounds, termination behavior | `tests/unit/test_agent_loop.py` (pending) | — | Not started |
| 56 | Adversarial/failure tests | Fabricated IDs, malformed args, DB failures | `tests/adversarial/*` (pending) | — | Not started |
| 57 | Live-model tests (gated) | Real API calls, opt-in only | `tests/live/*` (pending), gated by `RUN_LIVE_LLM_TESTS` | — | Not started |
| 58 | End-to-end tests | Natural request → parser or agent → tools → Postgres → observable result | `tests/e2e/test_command_api.py` (deterministic half done) | — | Partial (deterministic E2E done; agent-involving E2E pending) |

---

**Snapshot as of this update**: 37 backend tests passing (9 repository, 10
command-runner, 3 parser-to-runner, 15 parser unit, 4 API e2e — see actual
counts in `docs/PROGRESS.md` as they grow). Deterministic path is fully
done end-to-end (text → parser → CommandRunner → Postgres → HTTP response).
Agent/LangGraph/LLM/grounding-policy/memory-wiring/observability-emission/
frontend phases have their storage and config foundations in place but no
runtime behavior yet — next phase (LLM provider + first tool + LangGraph
loop) is where that starts, and it needs an `ANTHROPIC_API_KEY` (see the
checkpoint note in `docs/PROGRESS.md`).

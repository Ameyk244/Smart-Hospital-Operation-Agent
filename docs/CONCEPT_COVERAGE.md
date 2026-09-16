# Concept Coverage Audit

Maps each of the 58 required concepts (master prompt §18) to: why it exists
in this system, where it's implemented, how it works, what tests cover it,
and current status. Updated as each phase lands — not reconstructed at the
end. Status values: **Done** (implemented + tested + exercised by the
running app, including at least one real live-model run where applicable),
**Partial** (implemented but not fully exercised/tested yet), **Not
started**.

## LLM + tool calling

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 1 | Real LLM inference | The agent's actual reasoning/tool-selection engine | `app/agent/providers/anthropic_provider.py` | `tests/integration/test_live_agent.py` (real API calls, gated) | **Done** |
| 2 | LangChain model abstraction | Vendor-neutral chat model + message + tool-calling interface | `app/agent/providers/base.py` (`LLMProvider` returns a `BaseChatModel`) | same as above | **Done** |
| 3 | System prompting | Establishes the agent's role, tool set, and constraints | `app/agent/graph.py` (`SYSTEM_PROMPT`) | live tests show the model refusing to fabricate an ID per the prompt's instruction | **Done** |
| 4 | Structured tool/function calling | Real typed tool calls, never string-parsed pseudo-tools | `app/agent/tools/base.py` (`to_openai_tool_dict`, Pydantic `args_schema`) | `tests/integration/test_agent_loop.py`, live tests | **Done** |
| 5 | Model-driven tool selection | The LLM decides which tool(s) to call | `app/agent/graph.py` (`_make_agent_node`) | live tests (model chose `search_appointments` unprompted) | **Done** |
| 6 | Multiple tools | Search, action tools so far; more categories next phase | `app/agent/tools/{search,action}_tools.py` | `test_agent_loop.py` | **Done** (2 of 7 categories implemented; rest pending) |
| 7 | Tool registry | One place tools are declared and bound to the model | `app/agent/tools/base.py` (`TOOL_REGISTRY`) | — | **Done** |
| 8 | Tool execution layer | Validates args, checks grounding, executes, times out | `app/agent/graph.py` (`_make_tool_node`) | `test_agent_loop.py` (timeout, validation, grounding all covered) | **Done** |
| 9 | Tool results returned to the LLM | `ToolMessage` round-trip in the graph | `app/agent/graph.py` | `test_agent_loop.py::test_multi_round_search_then_reschedule` | **Done** |
| 10 | Multi-tool agent loop | Several tool calls across multiple rounds before finishing | `app/agent/graph.py` | `test_multi_round_search_then_reschedule` (search → reschedule → finish) | **Done** |

## LangGraph + orchestration

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 11 | LangGraph orchestration | Explicit, inspectable graph instead of a black-box agent call | `app/agent/graph.py` (`build_graph`) | `test_agent_loop.py` | **Done** |
| 12 | Conditional graph routing | tool-calls-present vs. finished vs. bounds-exceeded | `_route_after_agent`, `_route_after_tools` | bounded-execution tests | **Done** |
| 13 | Typed graph state | `AgentState` TypedDict with an explicit `add_messages` reducer | `app/agent/state.py` | all agent-loop tests | **Done** |

## Deterministic + agent architecture

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 14 | Deterministic fast path | Known requests never touch the LLM | `app/parser/parser.py` | `tests/unit/test_parser.py` | **Done** |
| 15 | LLM fallback | Unknown requests become eligible for the agent | `app/agent/eligibility.py`, `app/api/routes/chat.py` | `tests/e2e/test_chat_api.py` | **Done** |
| 16 | Command parser | Regex/keyword grammar over a fixed command set | `app/parser/parser.py` | `tests/unit/test_parser.py` | **Done** |
| 17 | Structured intents | `Command(name, args)`, not a free-text string | `app/execution/commands/base.py` | parser + command-runner tests | **Done** |
| 18 | Command runner | Single execution chokepoint for both entrances | `app/execution/commands/base.py` (`CommandRunner`) | `tests/integration/test_command_runner.py` | **Done** |
| 19 | Agent → existing execution path | Agent tools resolve to the same `Command`s the parser can build | `app/agent/tools/search_tools.py`, `action_tools.py` (both call `CommandRunner.execute`) | `test_agent_loop.py`, live tests | **Done** |
| 20 | Natural-language decomposition | An `execute_command` tool that re-parses a model-identified fragment through the same parser | `app/agent/tools/command_tools.py` (pending) | — | Not started |
| 21 | Shared command grammar | One `Command` vocabulary used by parser and agent tools alike | `app/execution/commands/*`, both entrances confirmed to hit the same handlers | `test_parser_to_runner.py` + `test_agent_loop.py` both exercise `search_appointments`/`reassign_scanner` | **Done** |

## Grounding + safety

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 22 | Grounding | Track which entity codes were actually exposed to a session | `app/agent/grounding.py` (`GroundingRegistry`), `SessionGroundedEntity` | `test_repositories.py`, `test_agent_loop.py` | **Done** |
| 23 | Grounding-by-rejection | A fabricated ID must be rejected in code, not by prompting | `app/agent/grounding.py` (`require_grounded` raises `GroundingRejectedError`) | `test_agent_loop.py::test_grounding_rejects_fabricated_appointment_code` (scripted) + `test_live_agent.py::test_live_grounding_rejects_fabricated_scanner` (real model, adversarial prompt) | **Done** |
| 24 | Argument validation | Tool args are Pydantic-validated before execution | `app/agent/tools/*` args_schema + `_make_tool_node`'s `model_validate` | `test_agent_loop.py::test_invalid_arguments_are_rejected` | **Done** |
| 25 | Read vs. action/write tools | Different risk profiles, different validation depth | `ToolSpec.is_write`; write tools additionally call `require_grounded` | `search_tools.py` vs `action_tools.py` | **Done** |
| 26 | Error-aware tools | Tool failures return structured errors the model can react to | `CommandError`/`CommandResult` → `ToolExecutionError` → `ToolMessage` | `test_command_runner.py` (modality/availability rejections), live tests | **Done** |

## Bounded agent execution

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 27 | Max rounds/tool-call limits | Prevents runaway loops | `app/config.py`, enforced in `app/agent/graph.py` | `test_max_rounds_terminates_a_looping_model`, `test_max_tool_calls_terminates_before_max_rounds` | **Done** |
| 28 | Timeouts | Model/tool/DB calls can't hang forever | `settings.llm_timeout_seconds`/`tool_timeout_seconds`, `asyncio.wait_for` around both the LLM call and each tool handler call | (timeout path is code-reviewed; a deterministic timeout test needs a handler that can be made to hang — tracked as a gap) | Partial |
| 29 | Invalid-call termination | Repeated bad tool calls end the session gracefully | `settings.max_invalid_tool_calls`, tracked per-run in `_make_tool_node` | `test_too_many_invalid_calls_terminates` (found and fixed a real bug: the check was skippable via `continue` for two of three invalid-call branches — see `docs/PROGRESS.md`) | **Done** |

## Async backend

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 30 | Python async programming | Real I/O concurrency for DB/LLM/tool calls, not decorative | Entire DB layer, `app/agent/graph.py` (`asyncio.wait_for` around real network calls), FastAPI routes | full test suite; live tests make real concurrent-capable async HTTP calls to Anthropic | **Done** |

## PostgreSQL + application data

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 31 | PostgreSQL | Primary datastore, real transactions | `docker-compose.yml`, `app/db/*` | full integration suite | **Done** |
| 32 | Repository/data-access layer | No SQL scattered through agent/tool code | `app/db/repositories/*` | `tests/integration/test_repositories.py` | **Done** |
| 33 | Transactions | Mutating commands commit-or-rollback atomically | `CommandRunner.execute`, each tool call's own session commit/rollback in `_make_tool_node` | `test_reassign_scanner_is_transactional_on_failure` | **Done** |

## State + memory

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 34 | Conversation/session identity | Every session has a stable id, created on first use regardless of entrance | `AgentSession`, `SessionRepository.get_or_create`, called from both `run_agent` and the chat route | `test_agent_loop.py`, `test_chat_api.py::test_session_id_is_reused_across_turns` | **Done** |
| 35 | Short-term conversation state | Durable transcript per session, actually read back into the next turn | `ConversationMessage`, `app/agent/history.py` (`to_langchain_messages`), wired into `app/api/routes/chat.py` | `test_session_id_is_reused_across_turns` (storage); live multi-turn context test tracked as a gap | **Done** (storage + injection wired); a test proving the agent actually *uses* prior turns is still pending |
| 36 | LangGraph checkpointing | Graph execution/resumption state, kept separate from conversation state | `langgraph-checkpoint-postgres` (dependency installed, not yet wired into `build_graph`) | — | Not started |
| 37 | Persistent preference memory | Explicit, session-scoped, never inferred | `Preference`, `PreferenceRepository` | `test_preference_remember_forget_roundtrip` | **Done** (storage layer); not yet injected into agent context |
| 38 | Explicit memory tools | `remember_preference`/`forget_preference`/`list_preferences` | `app/agent/tools/memory_tools.py` (pending) | — | Not started |
| 39 | Memory separation | Conversation / preferences / checkpoint never merged | Separate tables + separate code paths (`app/agent/history.py` only ever touches `ConversationMessage`) | — | **Done** |
| 40 | Memory injection | Prior conversation injected at the start of a run; preferences only when relevant | `app/api/routes/chat.py` (history), preference injection pending | — | Partial (conversation done, preferences not started) |
| 41 | Memory safeguards | Preferences can't be silently overwritten by inference | `PreferenceRepository.remember` is upsert-by-explicit-call only; nothing in the agent loop writes to it | — | **Done** at the repository boundary |

## Application context

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 42 | Current application context | "the next appointment", "that scanner" resolved from recent state | Conversation history now flows into the graph (`app/agent/history.py`); the grounding ledger is the code-level version of "what's currently in scope" | grounding tests | **Done** (grounding half); a test proving pronoun-style reference resolution across turns is tracked as a gap |
| 43 | Context updates after tools | Grounded set updates as tools run, within a single round | `app/agent/grounding.py` `expose()` called after every successful search/write | `test_multi_round_search_then_reschedule` (scanner grounded by search, then used by reschedule) | **Done** |

## Agent activation

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 44 | Agent eligibility gate | Decides whether an unmatched request is even allowed to reach the LLM | `app/agent/eligibility.py` | `test_chat_api.py::test_empty_request_is_rejected_before_reaching_the_agent` | **Done** |

## Configuration + providers

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 45 | Configuration layer | One typed source of truth for every setting | `app/config.py` | used throughout | **Done** |
| 46 | Secrets management | No secrets committed; `.env` + `.env.example` | `backend/.env.example`, `.gitignore` | manual verification | **Done** |
| 47 | Provider abstraction | Agent not welded to one LLM vendor | `app/agent/providers/{anthropic,openrouter}_provider.py` + `factory.py` | Anthropic path proven live; OpenRouter path implemented but not live-tested (no OpenRouter key provided) | **Done** (Anthropic); OpenRouter Partial |

## Observability

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 48 | Observability | Action-level execution trace | `app/observability/tracing.py` (`record_event`), called from every branch of `_make_agent_node`/`_make_tool_node` | visible in live test output (JSON lines per decision); `AgentEvent` rows queryable | **Done** |
| 49 | Structured logging | JSON logs for every action-level decision | `app/observability/logging_config.py` (structlog, JSON renderer) | live test output shows real JSON log lines | **Done** |
| 50 | Agent traceability (no hidden chain-of-thought) | Only observable actions are ever logged/stored | `AgentEvent`/`record_event` schema has no reasoning field — only event_type/tool_name/args/status/latency/error | — | **Done** |

## Testing

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 51 | Unit tests | Fast, no-DB, no-LLM tests | `tests/unit/test_parser.py` | — | **Done** |
| 52 | Mocked LLM tests | Deterministic agent-loop tests without live API calls | `tests/integration/test_agent_loop.py` (`ScriptedChatModel`, a `FakeMessagesListChatModel` subclass) | 7 tests, all offline | **Done** |
| 53 | Real tool integration tests | Tools actually hit the real DB | `test_command_runner.py`, `test_agent_loop.py` (tools called through the real graph against real Postgres) | — | **Done** |
| 54 | Parser → executor tests | Deterministic path, no LLM | `tests/integration/test_parser_to_runner.py` | — | **Done** |
| 55 | Agent-loop tests | Multiple tool rounds, termination behavior | `test_agent_loop.py` | 7 tests covering happy path + 3 termination modes | **Done** |
| 56 | Adversarial/failure tests | Fabricated IDs, malformed args, unknown tools | `test_agent_loop.py` (scripted) + `test_live_agent.py` (real model, adversarial prompt) | — | **Done** (DB-failure and malformed-JSON-from-model cases still pending) |
| 57 | Live-model tests (gated) | Real API calls, opt-in only | `tests/integration/test_live_agent.py`, `tests/e2e/test_chat_api.py::test_agent_path_via_http_with_live_model`, gated by `RUN_LIVE_LLM_TESTS` | run manually, all passing against real Claude | **Done** |
| 58 | End-to-end tests | Natural request → parser or agent → tools → Postgres → observable result | `tests/e2e/test_command_api.py` (deterministic), `tests/e2e/test_chat_api.py` (deterministic + live agent via real HTTP) | — | **Done** |

---

**Snapshot as of this update**: 54 backend tests (51 offline + 3 live-gated),
all passing; `ruff` clean. The full architecture diagram in
docs/ARCHITECTURE.md §1 is now real and demonstrated end-to-end with a live
model: a request either matches the deterministic parser and never touches
the LLM, or falls through the eligibility gate into a LangGraph loop that
does real structured tool calling, grounds every entity reference in the
database, enforces rejection of fabricated IDs, and is bounded on rounds/
tool-calls/invalid-calls — all reachable over real HTTP.

Two real bugs were caught and fixed by tests during this phase (see
`docs/PROGRESS.md` for detail): an invalid-call counter that silently never
reached its threshold for two of three rejection branches, and a
`FakeMessagesListChatModel` object-identity pitfall that made LangGraph's
message-merge silently truncate a scripted "looping model" test's
transcript.

**Remaining gaps going into the next phase**: tool categories 2 (command
decomposition), 6 (observation), and 7 (memory) aren't built yet; LangGraph
checkpointing isn't wired in; preference injection isn't wired in; and the
frontend doesn't exist yet.

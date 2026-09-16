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
| 6 | Multiple tools | All 7 tool categories from docs/ARCHITECTURE.md §4 now implemented | `app/agent/tools/{search,action,memory,command,observation}_tools.py` | `test_agent_loop.py`, `test_memory_tools.py`, `test_command_tools.py`, `test_observation_tools.py` | **Done** |
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
| 19 | Agent → existing execution path | Agent tools resolve to the same `Command`s the parser can build | `app/agent/tools/{search,action,command,observation}_tools.py` (all call `CommandRunner.execute`) | `test_agent_loop.py`, `test_command_tools.py`, live tests | **Done** |
| 20 | Natural-language decomposition | `execute_command` re-parses a model-identified fragment through the *same* deterministic parser | `app/agent/tools/command_tools.py` | `test_command_tools.py` (happy path + grounding chain), live-verified (model correctly issued two `execute_command` calls for a compound question) | **Done** |
| 21 | Shared command grammar | One `Command` vocabulary used by parser and agent tools alike; the grammar has **no rule that produces a write command**, so `execute_command` structurally cannot be a mutation backdoor | `app/execution/commands/*`, `app/parser/parser.py` (5 rules, all reads) | `test_parser_to_runner.py`, `test_command_tools.py::test_execute_command_cannot_be_used_to_mutate_data` (adversarial: scripted model tries to phrase a mutation as a command, verified unchanged afterward) | **Done** |

## Grounding + safety

| # | Concept | Why | Location | Test(s) | Status |
|---|---|---|---|---|---|
| 22 | Grounding | Track which entity codes were actually exposed to a session | `app/agent/grounding.py` (`GroundingRegistry`), `SessionGroundedEntity` | `test_repositories.py`, `test_agent_loop.py` | **Done** |
| 23 | Grounding-by-rejection | A fabricated ID must be rejected in code, not by prompting | `app/agent/grounding.py` (`require_grounded` raises `GroundingRejectedError`) | `test_agent_loop.py::test_grounding_rejects_fabricated_appointment_code` (scripted) + `test_live_agent.py::test_live_grounding_rejects_fabricated_scanner` (real model, adversarial prompt) | **Done** |
| 24 | Argument validation | Tool args are Pydantic-validated before execution | `app/agent/tools/*` args_schema + `_make_tool_node`'s `model_validate` | `test_agent_loop.py::test_invalid_arguments_are_rejected` | **Done** |
| 25 | Read vs. action/write tools | Different risk profiles, different validation depth | `ToolSpec.is_write`; write tools additionally call `require_grounded` — and so does the narrow `get_scanner_availability` read tool, proving grounding isn't only a write-path concern | `search_tools.py`/`command_tools.py` (reads) vs `action_tools.py`/`memory_tools.py` (writes); `observation_tools.py` (a grounded read) | **Done** |
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
| 35 | Short-term conversation state | Durable transcript per session, actually read back into the next turn | `ConversationMessage`, `app/agent/history.py` (`to_langchain_messages`), wired into `app/api/routes/chat.py` | `test_session_id_is_reused_across_turns` (storage) | **Done** |
| 36 | LangGraph checkpointing | Graph execution/resumption state, kept separate from conversation state (`ConversationMessage`) and preferences | `app/agent/checkpointer.py` (`AsyncPostgresSaver`), wired into `build_graph`/`run_agent` (optional `checkpointer` param; `history` is only used as a fallback when no checkpointer is active, to avoid duplicating messages neither source can recognize the other already has) | `tests/integration/test_checkpointing.py` (3 tests: resumption without manual history, per-turn bound reset despite a shared thread, thread isolation) | **Done** — verified live through the *real* running server (not a test override): two HTTP turns to `/api/chat` with the same `session_id`, second turn correctly answered "what did I just ask?" from checkpointed state alone |
| 37 | Persistent preference memory | Explicit, session-scoped, never inferred | `Preference`, `PreferenceRepository` | `test_preference_remember_forget_roundtrip`, `test_memory_tools.py` | **Done** |
| 38 | Explicit memory tools | `remember_preference`/`forget_preference`/`list_preferences` | `app/agent/tools/memory_tools.py` | `test_memory_tools.py::test_remember_then_list_preference`, `::test_forget_preference` | **Done** |
| 39 | Memory separation | Conversation / preferences / checkpoint never merged | Separate tables + separate code paths (`app/agent/history.py` only touches `ConversationMessage`; `memory_tools.py` only touches `Preference`; neither routes through `CommandRunner`) | — | **Done** |
| 40 | Memory injection | Prior conversation injected at the start of a run; preferences injected into the system message only when they exist | `app/agent/graph.py` (`_build_system_message`, called once per `run_agent`, not per round — refactored this phase specifically so it's inspectable via `state["messages"][0]`) | `test_memory_tools.py::test_existing_preferences_are_injected_into_system_message`, `::test_no_preferences_means_plain_system_prompt`; **verified live**: a preference remembered in one `run_agent` call was correctly recalled by a second, separate `run_agent` call for the same session_id, without even needing to call a tool | **Done** |
| 41 | Memory safeguards | Preferences can't be silently overwritten by inference | `PreferenceRepository.remember` is upsert-by-explicit-call only; nothing in the agent loop writes to it outside the `remember_preference` tool handler | — | **Done** |

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
| 48 | Observability | Action-level execution trace | `app/observability/tracing.py` (`record_event`), called from every branch of `_make_agent_node`/`_make_tool_node`; served to the UI via `GET /api/sessions/{id}/trace` (`app/api/routes/sessions.py`) | visible in live test output (JSON lines per decision); `test_operations_and_sessions_api.py` covers the trace endpoint | **Done** |
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
| 56 | Adversarial/failure tests | Fabricated IDs, malformed args, unknown tools, mutation-via-decomposition attempts, oversized client input | `test_agent_loop.py`, `test_command_tools.py::test_execute_command_cannot_be_used_to_mutate_data`, `test_observation_tools.py::test_get_scanner_availability_rejects_ungrounded_code`, `test_chat_api.py::test_oversized_session_id_is_rejected_with_a_clean_422` (scripted) + `test_live_agent.py` (real model, adversarial prompts) | — | **Done** (DB-failure and malformed-JSON-from-model cases still pending) |
| 57 | Live-model tests (gated) | Real API calls, opt-in only | `tests/integration/test_live_agent.py` (4 tests: search, adversarial grounding, adversarial preference injection across two independent runs), `tests/e2e/test_chat_api.py::test_agent_path_via_http_with_live_model`, gated by `RUN_LIVE_LLM_TESTS` | run manually, all passing against real Claude | **Done** |
| 58 | End-to-end tests | Natural request → parser or agent → tools → Postgres → observable result | `tests/e2e/test_command_api.py` (deterministic), `tests/e2e/test_chat_api.py` (deterministic + live agent via real HTTP) | — | **Done** |

---

**Snapshot as of this update**: 69 backend tests (65 offline + 4 live-gated),
all passing; `ruff` clean. The full architecture diagram in
docs/ARCHITECTURE.md §1 is real and demonstrated end-to-end with a live
model. **All 7 tool categories and all three memory concepts are now
implemented**, kept genuinely separate: conversation (`ConversationMessage`),
preferences (`Preference`), and LangGraph checkpointing (`AsyncPostgresSaver`)
each own a distinct persistence mechanism and a distinct purpose, verified
individually.

Real bugs caught and fixed during this project so far (see
`docs/PROGRESS.md` for detail): an invalid-call counter that silently never
reached its threshold for two of three rejection branches; a
`FakeMessagesListChatModel` object-identity pitfall that made LangGraph's
message-merge silently truncate a scripted "looping model" test's
transcript; an unconstrained client-supplied `session_id` that would have
hit the database as a raw 500 instead of a clean 422; a checkpointer test
isolation gap (its own Postgres tables aren't covered by `Base.metadata`,
so nothing cleared them between test runs); and a Windows-specific event
loop timing issue where `uvicorn app.main:app` creates its event loop
*before* importing the app module, making an in-app fix for psycopg's
Proactor-incompatibility too late — required a dedicated `run.py` entrypoint
instead.

**Remaining gap going into the next phase**: the frontend doesn't exist yet.

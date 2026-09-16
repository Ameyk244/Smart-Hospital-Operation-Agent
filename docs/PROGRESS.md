# Progress Log

See `docs/ARCHITECTURE.md` for the full plan and rationale. This file tracks
what's actually done, updated as work happens.

## Status: Phase 0 — repo scaffold

Started 2026-09-17.

### Completed
- Repo inspected: empty, no commits, `master` branch, clean.
- Environment inspected: Python 3.11.9 and 3.14.2 both available (using 3.11
  for ecosystem compatibility with LangChain/LangGraph); Node 24.13.0; Docker
  28.4.0 running (daemon confirmed, other unrelated containers already
  running on the host — left untouched).
- `docs/ARCHITECTURE.md` written: tech stack, data model, tool inventory,
  LangGraph shape, grounding design, memory separation, observability,
  13-phase plan.
- `docs/PROGRESS.md` (this file) created.

### Architectural decisions made so far (and why)
- **Python 3.11 over 3.14**: 3.14 is too new; some pinned LangChain/LangGraph
  transitive deps may lack wheels. Revisit if this turns out to be overly
  conservative.
- **CommandRunner as the single execution chokepoint**: both the deterministic
  parser and the agent's write tools construct a `Command` object and call
  the same `CommandRunner.execute()`. This directly satisfies the "no
  duplicated business logic" constraint in the spec and is unit-testable in
  isolation from both the parser and the LLM.
- **Postgres via Docker Compose**, not a cloud instance — keeps the project
  self-contained and free to run offline (aside from live LLM calls).
- **Anthropic as default LLM provider, OpenRouter as the documented
  alternative** — a thin `LLMProvider` abstraction in
  `app/agent/providers/` selects between them via `LLM_PROVIDER` env var, so
  the agent isn't welded to one vendor.

### Known checkpoint coming up
Phase 5 (LLM provider abstraction) can be built and unit-tested with a fake
model, but exercising it live requires an API key — will stop and ask per
the spec's §15 checkpoint format once that phase is reached.

## Status: Phase 1-2 complete — schema, seed data, repository layer

### Completed
- `backend/` scaffolded: `requirements.txt`/`requirements-dev.txt` (version
  ranges, not exact pins — exact pins conflicted between langchain/langgraph/
  langchain-anthropic on langchain-core; pip resolves a compatible set),
  `.env.example`, `app/config.py` (pydantic-settings), Python 3.11 venv.
- `docker-compose.yml`: Postgres 16, **host port 5433, not 5432** — this dev
  machine already has a native PostgreSQL service bound to 5432 (discovered
  via `Get-NetTCPConnection`; a second process was silently answering on
  "localhost" and producing password-auth failures that looked unrelated to
  the port at first). `.env`/`.env.example` updated to match.
- 9-table schema (`app/db/models/hospital.py`, `app/db/models/agent.py`) +
  initial Alembic migration, applied successfully.
- Repository layer: one file per aggregate
  (`catalog_repository.py` [departments/staff/rooms],
  `scanner_repository.py`, `patient_repository.py`,
  `appointment_repository.py`, `session_repository.py` [sessions +
  conversation messages], `preference_repository.py`,
  `grounding_repository.py`, `event_repository.py`).
- Reproducible synthetic seed generator (`app/seed/seed_data.py`, fixed RNG
  seed `20260917`) — 4 departments, 12 staff, 7 rooms, 8 scanners (with
  deliberate AVAILABLE/IN_USE/MAINTENANCE mix), 30 patients, 29 appointments
  including 4 DELAYED MRI and 3 DELAYED CT appointments with multiple
  compatible AVAILABLE scanners each — the ambiguity grounding-by-rejection
  needs to mean something. Idempotent (deletes hospital-domain rows in
  FK-safe order before reseeding; never touches agent-domain tables).
- Test infra: `tests/conftest.py` (per-test Postgres database/engine/
  transaction, rolled back after each test — see design note in that file
  for why engines are *not* shared across tests), `pytest.ini`.
  `tests/integration/test_repositories.py`: 9 tests, all passing, stable
  across repeated runs.

### Bugs found and fixed during this phase (real lessons, not hypothetical)
- **asyncpg deadlock across event loops**: an early version shared a
  session-scoped SQLAlchemy engine across pytest-asyncio's per-test event
  loops. This doesn't raise — it deadlocks silently (queries sit "active" in
  `pg_stat_activity` forever). Fixed by giving every test its own engine,
  created and disposed within that test's own event loop. Documented in
  `tests/conftest.py` so it doesn't get "optimized" back into a shared
  fixture later.
- **SQLAlchemy identity-map staleness**: after mutating a row (e.g.
  `reassign_scanner`) and re-querying it *within the same session*,
  SQLAlchemy returned the cached pre-mutation object instead of fresh data —
  by design, not a bug in SQLAlchemy, but wrong for this application: an
  agent round reuses one session across multiple repository calls (search
  tool, then a write tool's own lookup), and must see its own writes. Fixed
  by adding `.execution_options(populate_existing=True)` to every read query
  across full-entity repositories. Column-only queries (grounding ledger)
  don't need it — nothing there is ever mutated in place.
- **Nondeterministic message/event ordering**: `ORDER BY created_at` alone
  ties when two rows are written in the same millisecond (common when a
  route appends two messages back to back). Fixed by adding `id` as a
  tiebreaker in both `session_repository.get_recent_messages` and
  `event_repository.list_for_session`.

## Status: Phase 3-4 complete — trusted execution path + thin vertical slice (deterministic half)

### Completed
- `app/execution/commands/base.py`: `Command`, `CommandResult`, `CommandError`,
  `CommandRunner` — the single chokepoint both the parser and (later) the
  agent's write tools call through. Unknown commands are rejected before
  touching the DB; business-rule failures (`CommandError`) and unknown-entity
  failures (`ValueError`) are caught and turned into `CommandResult(success=
  False, ...)` with an `error_category`, rather than raising to the caller.
- 9 command handlers across `reference_commands.py`, `patient_commands.py`,
  `appointment_commands.py`: `list_departments`, `list_scanners`,
  `get_scanner_availability`, `search_patients`, `list_patient_appointments`,
  `show_next_appointment`, `search_appointments`, `list_delayed_appointments`,
  and the one mutating command, `reassign_scanner` (enforces modality match +
  AVAILABLE status; backs both the future `reschedule_appointment` and
  `assign_scanner` agent tools from one implementation).
- Deterministic parser (`app/parser/parser.py`): 5 grammar rules (list
  departments/scanners, show patient, show next appointment, list delayed
  appointments), first-match-wins, case-insensitive. Deliberately kept small
  — see the module docstring for why this must stay a fixed grammar rather
  than grow into partial NLU.
- FastAPI app (`app/main.py`, `app/api/routes/commands.py`): `POST
  /api/commands` runs parser → CommandRunner → JSON response.
  Manually smoke-tested against the real dev DB via a live `uvicorn` run
  (not just the test suite) — confirmed real HTTP round trip.
- Test growth: 10 command-runner tests, 3 parser-to-runner tests, 15 parser
  unit tests, 4 API e2e tests — 37 backend tests total, all passing, `ruff`
  clean.
- `docs/CONCEPT_COVERAGE.md` created, all 58 concepts listed with honest
  current status (12 Done, a few Partial, rest Not started).

### Bugs found and fixed during this phase
- `AppointmentRepository.reassign_scanner` returned the mutated ORM object
  directly; its `.scanner` relationship still pointed at the pre-mutation
  Scanner (setting `.scanner_id` doesn't auto-refresh an already-loaded
  relationship). Fixed by re-fetching via `get_by_code` (which has
  `populate_existing=True`) before returning.

### CHECKPOINT — secret needed to proceed into the agent phase

Phase 5+ (LLM provider abstraction, first real tool, LangGraph loop) needs a
live LLM credential to be more than unit-tested scaffolding. Specifically:

1. **What**: an Anthropic API key.
2. **Why**: `app/agent/providers/anthropic_provider.py` (next phase) makes
   real structured tool-calling requests to Claude — the master prompt
   explicitly disallows simulating this with string-matching or fake
   responses in the running app (mocks are for the offline test suite only).
3. **Where to get it**: https://console.anthropic.com/settings/keys
4. **Where to put it**: `backend/.env`, as `ANTHROPIC_API_KEY=<key>`
   (`backend/.env.example` already documents this; `.env` is gitignored).
5. **Env var name**: `ANTHROPIC_API_KEY` (already read by `app/config.py`).
6. **How to verify**: once set, `RUN_LIVE_LLM_TESTS=1 pytest -m live_llm`
   (once that test module exists in the next phase) will make one real call
   and confirm a response comes back; short of that, the provider module
   itself will fail fast at startup with a clear error if the key is missing
   when `LLM_PROVIDER=anthropic`.

Until this is supplied: the LangGraph loop, tool registry, and agent nodes
can still be built and unit-tested with a fake chat model — that work is not
blocked. Only the actual live end-to-end agent run is blocked. Proceeding on
that basis unless told otherwise.

## Status: Phase 5-7 complete — LLM provider, first tools, full LangGraph loop (thin vertical slice, complete)

The user supplied `ANTHROPIC_API_KEY` and it was verified live before
building on top of it. The full architecture diagram from
docs/ARCHITECTURE.md §1 is now real, both branches, proven end-to-end with a
live model over real HTTP — this is the "thin vertical slice" milestone from
the master prompt's §0/§20.

### Completed
- **Provider abstraction** (`app/agent/providers/`): `AnthropicProvider`
  (default) and `OpenRouterProvider` (OpenAI-compatible base URL), selected
  by `LLM_PROVIDER` via `factory.get_provider`; `get_default_chat_model()`
  lazily constructs and caches the process-wide model so a deterministic-
  only request never needs an API key.
- **Tool framework** (`app/agent/tools/base.py`): `ToolSpec` + `TOOL_REGISTRY`,
  hand-rolled rather than LangChain's `@tool`/`ToolNode` — deliberately, so
  the validation → grounding → timeout → observability pipeline around every
  call is visible in this codebase, not inside a framework black box.
- **Grounding-by-rejection** (`app/agent/grounding.py`): `GroundingRegistry`
  wraps the ledger repository; write tools call `require_grounded()` before
  building a `Command`. Proven against both a scripted fake model and a real
  adversarial prompt to live Claude that tried to skip searching first.
- **Two real tools**: `search_appointments` (read, exposes appointment/
  scanner/patient codes to the grounding ledger) and `reschedule_appointment`
  (write, requires both codes grounded, routes through the same
  `Command("reassign_scanner", ...)` the deterministic parser could build).
- **Observability** (`app/observability/`): `structlog` JSON logging +
  `AgentEvent` rows, emitted at every decision point in the graph — visible
  live in test output.
- **The LangGraph loop** (`app/agent/graph.py`): two nodes (`agent`, `tools`),
  typed `AgentState`, conditional routing, and all four bounds (max rounds,
  max tool calls, LLM/tool timeouts, max invalid calls) enforced explicitly.
- **Eligibility gate** (`app/agent/eligibility.py`) and a unified
  `POST /api/chat` endpoint (`app/api/routes/chat.py`) tying parser and agent
  into one request flow, with conversation history persisted and fed back
  into the next turn regardless of which path handled a given request.
- Test growth: 7 scripted agent-loop tests (mocked LLM, concept 52), 3
  gated live-model tests (concept 57, run manually against real Claude — all
  passing), plus e2e coverage for both the deterministic and agent paths
  through real HTTP. **54 backend tests total, `ruff` clean.**

### Real bugs found and fixed during this phase
- `AppointmentRepository.reassign_scanner` (carried over from last phase,
  fixed here) needed a second look once tools called it indirectly — no new
  issue, confirms the earlier fix held.
- **Invalid-call termination had a real gap**: `_make_tool_node`'s
  `max_invalid_tool_calls` check sat after the try/except block, but the
  "unknown tool" and "invalid arguments" branches used `continue` to skip
  past it — a model that only ever called unknown tools would loop all the
  way to `max_agent_rounds` instead of stopping at `max_invalid_tool_calls`.
  Caught by `test_too_many_invalid_calls_terminates`. Fixed by adding the
  check to both branches (with a comment explaining why, so a future new
  branch doesn't reintroduce the same gap).
- **`FakeMessagesListChatModel` + LangGraph `add_messages` interaction**: a
  test that reused one `AIMessage` object across every cycle broke silently
  — LangGraph assigns an id to a message the first time it's merged into
  state (mutating the object in place), so the *same* object handed back on
  the next round was treated as an in-place update, not a new append, and
  the transcript stopped growing without any error. Fixed by generating
  distinct message instances per round in the test helper, documented in
  the test file so the pitfall doesn't get reintroduced.
- **`agent_events` FK violation**: `run_agent` recorded the first `AgentEvent`
  before any `AgentSession` row existed for a fresh session id. Fixed by
  having `run_agent` call `SessionRepository.get_or_create` as its first
  action, regardless of which caller (API route, test, script) invoked it.

### Verified live (not just unit-tested)
Ran `RUN_LIVE_LLM_TESTS=1 pytest -m live_llm -v -s` against real Claude:
search → real tool call → real Postgres query → coherent formatted answer
citing actual seeded appointment/scanner codes; and a live adversarial
prompt asking the model to skip searching and act on a fabricated ID
directly — the model declined per the system prompt, and grounding would
have rejected it in code regardless if it hadn't.

## Status: Phase 8a complete — memory tools + preference injection

Reviewed with the user before proceeding (per their explicit request);
confirmed to proceed with memory tools next, deferring checkpointing and
the frontend.

### Completed
- **Structural fix first**: the system prompt used to be silently
  re-prepended inside `agent_node` every round, checking `messages[0]` and
  never actually landing in `AgentState` — impossible to test or inspect
  from outside. Refactored so `run_agent` builds the system message once
  (via `_build_system_message`) and puts it in `initial_state["messages"][0]`
  directly; `agent_node` now just calls the model with `state["messages"]`
  as-is. This is what made preference injection actually testable.
- **Memory tools** (`app/agent/tools/memory_tools.py`): `remember_preference`,
  `forget_preference`, `list_preferences`. Deliberately do **not** route
  through `CommandRunner` — preferences are a distinct memory concern
  (docs/ARCHITECTURE.md §7), not a hospital-operations command, and forcing
  them through the command grammar would blur that separation for no
  benefit.
- **Preference injection** (concept 40): `_build_system_message` loads a
  session's preferences and appends them to the system prompt only when any
  exist — verified both in scripted tests and **live**, across two separate
  `run_agent` calls sharing one session_id: the first remembered "prefer MRI
  scanners", the second (a fresh run) correctly answered "MRI" from the
  injected system message alone, without even calling `list_preferences`.
- 4 new tests (`tests/integration/test_memory_tools.py`): remember→list
  round trip, forget, injection-present, injection-absent. **58 backend
  tests total (55 offline + 3 live-gated), all passing, ruff clean.**

## Status: Phase 8b complete — command decomposition + observation tool (all 7 tool categories done)

### Completed
- **Regression test preserved**: the ad-hoc live preference-injection check
  from the previous phase is now a permanent gated test
  (`test_live_preference_injection_across_separate_runs`) — two independent
  `run_agent` calls sharing one session_id, verified live.
- **`execute_command`** (`app/agent/tools/command_tools.py`, concepts 19/20/21):
  lets the agent decompose a natural-language sub-instruction into the same
  parser + `CommandRunner` the deterministic path uses. Architectural
  property made explicit and tested: the deterministic grammar has **no
  rule that builds a write `Command`**, so `execute_command` cannot be used
  as a mutation backdoor regardless of phrasing — proven with an adversarial
  test that scripts a model trying exactly that, then verifies via a second,
  independent `execute_command` call that nothing changed.
  Live-verified: asked a real compound question ("what departments exist,
  and how many scanners are available"), the model correctly issued two
  separate `execute_command` calls and synthesized both results.
- **`get_scanner_availability`** (`app/agent/tools/observation_tools.py`,
  the observation/read category): deliberately small — its purpose is
  proving grounding-by-rejection applies to a *read* of a specific entity,
  not only to writes. Looking up an invented scanner code is rejected the
  same way rescheduling onto one would be.
- **Real bug found via a schema constraint, fixed at the boundary**: writing
  tests with descriptive session_id strings hit
  `StringDataRightTruncationError` — `AgentSession.id` is `String(36)`,
  sized for a UUID, and nothing validated a client-supplied `session_id`
  before it reached the database. Fixed by adding `max_length=36` to
  `ChatRequest.session_id`, turning an unhandled 500 into a clean 422;
  added `test_oversized_session_id_is_rejected_with_a_clean_422`.
- All 7 tool categories from docs/ARCHITECTURE.md §4's original inventory
  are now implemented. 8 new tests. **66 backend tests total (62 offline +
  4 live-gated), all passing, ruff clean.**

## Status: Phase 9 complete — LangGraph Postgres checkpointing

This was the piece flagged in advance as most likely to eat unexpected
time, and it did surface two genuine platform-specific issues — both real,
both fixed, neither a dead end.

### Completed
- **`app/agent/checkpointer.py`**: `AsyncPostgresSaver` wrapped in a small
  context manager, converting our asyncpg-style `DATABASE_URL` to the
  psycopg connection string format the checkpointer package needs.
- **`build_graph`/`run_agent`** now accept an optional `checkpointer`. When
  present: `thread_id=session_id`, and manual `history` (from
  `ConversationMessage`) is *not* also injected — passing both would
  duplicate the transcript, since freshly-constructed LangChain messages
  have no id the checkpoint could use to recognize them as already present
  (the same class of pitfall as the `FakeMessagesListChatModel` bug from
  the agent-loop phase). round/tool/invalid-call counters and
  `terminated_reason` are explicitly reset every call regardless — those
  bounds are per-turn, not per-thread-lifetime, and checkpointing would
  otherwise silently accumulate them across a whole conversation.
- **Real app wiring, not just tests**: `app/main.py` now builds the
  checkpointer once via a FastAPI `lifespan`, stored on `app.state`; `/api/
  chat` reads it through a `get_checkpointer` dependency (overridable in
  tests exactly like `get_db`).
- 3 new tests (`tests/integration/test_checkpointing.py`): resumption
  without manual history, per-turn bound reset despite a shared thread,
  thread isolation.

### Two real platform-specific bugs found and fixed
1. **psycopg async vs. Windows' default event loop**: `psycopg`'s async
   mode refuses to run under `ProactorEventLoop` (Windows' default since
   Python 3.8). Fixed for tests by setting
   `asyncio.WindowsSelectorEventLoopPolicy()` at `conftest.py` import time
   (before pytest-asyncio creates any loop).
2. **The same fix didn't work for the actually-served app**: `uvicorn
   app.main:app` calls `asyncio.run()` — creating its event loop — *before*
   it imports the app string, so a policy fix inside `app/main.py` itself
   is set too late to matter (confirmed by reproducing the exact
   `psycopg.InterfaceError` via a live server start). Fixed with a
   dedicated `backend/run.py` entrypoint that sets the policy before ever
   calling into uvicorn. Verified with the real, non-overridden lifespan
   checkpointer: two HTTP turns to a running server, same `session_id`,
   second turn correctly answered "what did I just ask you?" — genuine
   proof through the actual serving path, not a test double.
3. **Checkpointer test isolation**: its own Postgres tables
   (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) are created by
   `AsyncPostgresSaver.setup()`, not `Base.metadata`, so nothing cleared
   them between test runs — a fixed thread_id reused across two runs of the
   same test silently resumed the *previous run's* checkpoint data. Fixed
   by truncating those tables in the `checkpointer` fixture.
- Also bumped `langgraph` from the original `0.2.x` pin to `0.6.x` (the
  smallest change that satisfies `langgraph-checkpoint-postgres`'s own
  version check, avoiding a much larger, riskier jump to the materially
  different 1.x API) — full offline suite re-verified clean after the bump
  before proceeding.

**69 backend tests total (65 offline + 4 live-gated), all passing, ruff
clean.**

## Status: Phase 9.5 complete — read/trace API surface for the frontend

Before delegating frontend work, closed a real gap: only `/api/commands`
and `/api/chat` existed — nothing to browse hospital data directly, and no
endpoint for the UI's trace panel to read `AgentEvent` rows from. Built
these first, in the main session (not delegated), since the frontend
subagent needs a genuinely complete, stable API surface to build against
rather than discovering gaps mid-build.

### Completed
- `app/api/routes/operations.py`: `GET /api/operations/{departments,
  scanners,appointments,patients}` — read-only, all built as thin
  `CommandRunner.execute(Command(...))` calls, the *third* caller of the
  same command vocabulary (alongside the parser and the agent's tools).
- `app/api/routes/sessions.py`: `GET /api/sessions/{id}/trace` (the
  action-level `AgentEvent` history a session's agent activity produced)
  and `GET /api/sessions/{id}/messages` (conversation history, for
  reloading a chat panel). Deliberately polling-based, not
  streaming/SSE — documented in the module docstring why: a live-streaming
  trace would need `record_event` to also push into a pub/sub channel,
  meaningfully more infrastructure than this project's complexity budget
  should spend on a transport layer rather than the agent architecture
  itself.
- Consolidated a `client` httpx fixture (previously duplicated across two
  e2e test files) into `tests/conftest.py` as a shared fixture.
- 6 new e2e tests. **72 backend tests total (68 offline + 4 live-gated),
  all passing, ruff clean.**

### Next
Phase 10: the frontend. Delegating to a subagent per the master prompt's
§12 (clearly separated responsibility, the API surface is now stable) —
explicit instruction to wire the trace panel to the real
`/api/sessions/{id}/trace` endpoint, not a mocked one.

## Concepts covered so far
- **31. PostgreSQL** — `docker-compose.yml`, live schema.
- **32. Repository/data-access layer** — `app/db/repositories/*`.
  Tested: `tests/integration/test_repositories.py`.
- **33. Transactions** — `AppointmentRepository.reassign_scanner` mutates
  multiple columns under one flush; test fixtures themselves rely on
  transactional rollback for isolation. Real multi-statement transactional
  writes land in Phase 3 (`CommandRunner`).
- **30. Python async programming** — the entire DB layer is async
  (`asyncpg` + SQLAlchemy async engine); not yet exercised concurrently
  (that's the FastAPI/agent phases), but the foundation is real, not
  decorative.
See `docs/CONCEPT_COVERAGE.md` (created in Phase 3) for the full audit
table format going forward.

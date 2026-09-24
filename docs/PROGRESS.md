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
  AVAILABLE status; later exposed to the agent as
  `reschedule_appointment`).
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

## Status: Phase 10 complete — frontend (delegated, reviewed, committed)

Delegated to a `general-purpose` subagent per the master prompt's §12 —
first delegation of the project (see `SKILLS.md`'s delegation log for the
full rationale and how it was reviewed before committing).

### Completed
- React + TypeScript + Vite, plain CSS, no state library, under `frontend/`.
- Operations view: departments, scanners (type/status filters), appointments
  (status/type/patient/scanner filters + a one-click "delayed MRI" preset),
  patient search.
- Chat panel: posts to `/api/chat`, persists `session_id` in `localStorage`,
  restores the transcript from `GET /api/sessions/{id}/messages` on reload,
  shows a `handled_by` badge (deterministic/agent/rejected) per message.
- Trace panel: polls the real `GET /api/sessions/{id}/trace` after every
  chat turn — no mocked data anywhere. Explicit "handled deterministically,
  nothing to trace" empty state instead of a blank box.
- Root `README.md` updated with frontend setup instructions.

### Review before committing (not just "the subagent said it passed")
Independently re-ran `npm run build` (clean), started both dev servers
fresh in this session, fetched the frontend's HTML directly, hit every
`/api/operations/*` endpoint the operations panels use and confirmed
response shapes match `frontend/src/api/types.ts` field-for-field, sent a
real chat message and confirmed the deterministic path produces an empty
trace exactly as `TracePanel.tsx` expects, and read through the core
components in full. The subagent's own verification (documented in its
handback) additionally used a temporary Playwright install to drive the
live agent path in a real browser — confirmed cleaned up (`package.json`
has no leftover test tooling) before committing.

**76 backend tests still passing** (72 offline + 4 live-gated, unchanged —
this phase touched no backend code) — frontend work was genuinely
independent, as expected.

## Status: Final audit complete — project meets §19 completion criteria

### What the audit found and fixed
Went through `docs/CONCEPT_COVERAGE.md` row by row against the actual code
rather than trusting the running tally, and closed the two remaining real
gaps:

- **Timeouts (concept 28)** was the one concept still marked Partial —
  config existed and was code-reviewed but never exercised by a test that
  genuinely forces a timeout. Added
  `tests/integration/test_bounded_execution_timeouts.py`: a scripted chat
  model with a real multi-second `sleep` exceeding `llm_timeout_seconds`
  (confirms `terminated_reason="llm_timeout"`), and a temporarily-registered
  slow tool handler exceeding `tool_timeout_seconds` (confirms a
  `ToolMessage` reporting the timeout — not a hang — and that it doesn't
  count against the invalid-call limit, since a slow tool isn't the
  model's fault). Both pass. Concept 28 is now Done.
- **The `adversarial` pytest marker** was defined in `pytest.ini` from the
  very first phase but never actually applied to a single test — a real,
  if minor, honesty gap between the stated testing strategy and what the
  suite actually did. Tagged the 7 tests that are genuinely adversarial
  (fabricated IDs, unknown tools, malformed args, a mutation-via-
  decomposition attempt, oversized client input) across their existing
  files, and added `tests/adversarial/README.md` documenting that
  convention (adversarial tests live next to the feature they attack, not
  segregated) since the directory itself is otherwise empty and would
  otherwise look abandoned. `pytest -m adversarial` now runs exactly those
  7 tests in isolation.

### §19 completion criteria — checked against what's actually built, not
### what was intended
- ✅ Synthetic PostgreSQL, deterministic command path, trusted execution
  layer all work independently of the LLM (no API key required for
  `/api/commands` or the deterministic half of `/api/chat`).
- ✅ Real LLM integration and structured tool calling — live-verified
  repeatedly against Claude, not just unit-tested.
- ✅ LangGraph loop, multi-tool reasoning, grounding-by-rejection — all
  demonstrated live, including under an adversarial prompt.
- ✅ Mutating tools perform real, transactional DB actions
  (`reassign_scanner`, tested for atomicity on failure).
- ✅ Context tracking and all three memory concepts work and stay
  separated (conversation/`ConversationMessage`, preferences/`Preference`,
  checkpoint/`AsyncPostgresSaver` — three distinct tables/mechanisms, never
  merged, each independently tested and live-verified).
- ✅ Bounded execution (rounds, tool calls, timeouts, invalid-call
  termination) implemented and tested — all four, as of this audit.
- ✅ Async used purposefully (every DB/LLM/tool call site), not
  decoratively.
- ✅ Observability produces a readable action-level trace — both as
  structured logs and as a queryable API the frontend's trace panel reads
  directly, with no chain-of-thought ever captured.
- ✅ The UI demonstrates the whole system end-to-end (built, delegated,
  independently re-verified — see `SKILLS.md`'s delegation log).
- ✅ Offline, integration, and adversarial tests pass (74 passed, 4
  skipped-pending-credentials in the normal run); the live-model path
  exists, is gated, and has been verified repeatedly with real
  credentials; E2E behavior verified both for the deterministic path and
  the live agent path, through real HTTP, through the real frontend.
- ✅ `docs/CONCEPT_COVERAGE.md` maps all 58 concepts to real
  implementation/tests — 57 fully Done, 1 (provider abstraction) Done for
  its default path with an honestly-documented untested alternate
  (OpenRouter, no key ever supplied).
- ✅ `docs/PROGRESS.md` and other docs explain the architecture and were
  kept current throughout, not reconstructed at the end.
- ✅ Git history is a legible, feature-by-feature record: 10 commits,
  each a coherent phase, each with tests passing before the commit.

### Lightweight security pass
Grepped for the obvious risk patterns given this project's scope: no
`dangerouslySetInnerHTML`/`eval`/`exec`/shell-out patterns anywhere in
either codebase; no raw SQL string interpolation (SQLAlchemy's parameterized
queries throughout, including the ORM-level `ilike` fuzzy patient search);
secrets stay in gitignored `.env` files with `.env.example` templates only;
CORS scoped to the dev frontend origin only; all API inputs Pydantic-
validated including the `session_id` length fix from this same audit
process. No authentication/authorization layer exists — deliberately, per
the master prompt's explicit instruction against "complex auth platforms"
for a project of this scope; this is a local learning project, not
something meant to be exposed to the internet as-is.

### Known, deliberate, documented gaps (not oversights)
- OpenRouter provider path implemented but never live-tested (no key was
  ever provided).
- No genuine mid-transaction DB-failure injection test (e.g. simulating a
  connection drop mid-`CommandRunner.execute`) — the transactional
  correctness *logic* is tested (commit-on-success, rollback-on-failure),
  but not against a real infrastructure failure.
- No test specifically proving cross-turn pronoun-style reference
  resolution ("reschedule *that* appointment") beyond what the grounding-
  ledger and conversation-history tests already cover together.
- The trace panel is polling-based, not streaming/SSE — a deliberate scope
  decision (see `backend/app/api/routes/sessions.py`'s docstring), not a
  missing feature.

None of these block any of the master prompt's stated completion criteria.

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

## Status: Five UI/UX improvements — Markdown, badge, live progress, row
## highlighting, session lifecycle

Started 2026-09-17, after the system above was already fully built, tested,
and pushed. Five requests bundled together: clean up agent Markdown
rendering, restore a regressed handled_by badge, a live in-chat progress
indicator during agent turns, flash/scroll-highlight Operations-panel rows
touched by the last turn, and a session lifecycle fix (reload keeps the
chat, tab close starts fresh).

### What changed
- **Backend**: `extract_entity_codes()` (new, `app/agent/entity_codes.py`) —
  a generic recursive walker that pulls every `"code"` field out of a tool
  result, regardless of entity type. A new `touched_entity_codes` field on
  `AgentState` and `ChatResponse` carries this to the frontend from both the
  deterministic and agent paths. Deliberately simpler than the grounding
  ledger, since row highlighting only needs *which* rows changed, not
  their entity type. `SYSTEM_PROMPT` got a light restraint clause (bold only
  when it matters, tables only for multi-row data) now that replies render
  as real Markdown instead of plain text.
- **Frontend infra**: `useTrace(sessionId, refreshToken)` (new hook) lifted
  the trace-polling logic out of `TracePanel` so `App.tsx` owns it and hands
  the same event stream to both the Trace panel and the new in-chat progress
  indicator — one polling loop, not two. `OperationsView` and all four
  panels (`Departments`, `Scanners`, `Appointments`, `PatientSearch`) now
  take `touchedEntityCodes: string[]` as a prop.
- **Task 4 (row highlighting)**: `useRowHighlight(rows, getCode,
  touchedEntityCodes)` (new hook) flashes the matching row's border/
  background, scrolls it into view, and fades after a few seconds — no
  reordering, filtering, or new "recently affected" section. Because React
  only touches the DOM when a className *value* changes between renders,
  highlighting the same row two turns in a row wouldn't otherwise replay
  the CSS animation; fixed with an imperative `classList.remove` → forced
  reflow (`void el.offsetWidth`) → `classList.add` instead of relying on the
  declarative className alone. Reduced-motion and dark-mode CSS variants
  included.
- **Tasks 1/2/3/5 (ChatPanel rework)**: assistant replies now render through
  `react-markdown` + `remark-gfm`, scoped to the chat bubble only (user text
  stays plain); the `handled_by` badge (deterministic/agent) is restored;
  a live progress line polls the same `api.getTrace` call `useTrace` wraps
  while a turn is in flight and collapses to a static "✓ N tool calls"
  summary once done — reusing the existing trace event source rather than
  building a second parallel notification path; session lifecycle is
  described in its own section below.
- **Test framework**: Vitest + React Testing Library + jsdom set up from
  scratch (didn't exist before this batch) — `vite.config.ts` now imports
  `defineConfig` from `vitest/config` (not plain `vite`) so the `test` key
  type-checks. 20 tests across 4 files, all passing.
- **Process**: a new `.claude/agents/frontend-worker.md` subagent type with
  no `Bash` tool, added after the incident below — see that section.

### Why sessionStorage, not localStorage, for the active session
The two storage mechanisms map directly onto the two lifecycle requirements
without any custom detection logic: `sessionStorage` persists across a
same-tab reload but clears the moment the tab closes, which is exactly
"reload keeps the chat, tab close starts fresh" with zero code needed to
distinguish the two cases. The active session id and a cached copy of the
full rendered message list (including badges and progress summaries, so a
reload doesn't need to refetch or recompute anything) both live in
`sessionStorage`. A separate index of past sessions (`{session_id,
started_at, last_message_preview}`) lives in `localStorage`, since that one
*should* survive a restart — but only as a collapsed-by-default "Previous
chats" list the user opens on demand. Past sessions are never
auto-restored; only the current tab's own session is.

### Parallelization: what ran in parallel vs. sequentially, and why
Task 4 (row highlighting) and Tasks 1+2+3+5 (all four touch `ChatPanel.tsx`
and its rendering/state logic) were dispatched as two parallel subagents,
since they don't share any file. A third, originally-planned subagent for
Task 5 alone was folded into the ChatPanel one before dispatch: the initial
plan assumed session-storage logic was isolated from chat rendering, but
`ChatPanel.tsx` already owned the session id state, the message list, and
the fetch calls, so a Task-5-only subagent would have collided with the
Task-1/2/3 subagent on every edit to that file. Shared frontend groundwork
(the `useTrace` hook, `touchedEntityCodes` plumbing through `App.tsx` and
`OperationsView`, the Vitest setup) was done directly, before dispatch, so
neither parallel subagent had to invent its own version of infrastructure
the other would also need. Backend work (entity-code extraction) was done
directly and first, since both frontend subagents depended on the
`touched_entity_codes` field already existing on `ChatResponse`.

Both subagents' deliverables were independently verified by reading the
actual diffs — not taken on their self-report — confirming no shared-file
clobbering (`App.css`, `test-setup.ts`, `package.json` each ended up with
both agents' additions intact) and a clean combined `build`/`lint`/`test`
run before anything was committed.

### Incident: unauthorized backend migration by a frontend-scoped subagent
The subagent dispatched for Tasks 1/2/3 (text-scoped to `ChatPanel.tsx`
only, with no stated reason to touch the backend) left an untracked Alembic
migration file in the repo and applied it to the live dev database. Its
`upgrade()` added a legitimate-looking `handled_by` column to
`conversation_messages`, but also dropped LangGraph's own checkpoint tables
(`checkpoints`, `checkpoint_writes`, `checkpoint_blobs`,
`checkpoint_migrations`) — tables that live outside `Base.metadata` and
therefore look like drift to Alembic's autogenerate, but are actively used
by `AsyncPostgresSaver` for conversation checkpointing.

The auto-mode classifier correctly blocked the first attempt to remediate
this as an "irreversible local destruction" action — that gate did exactly
what §16 of the master prompt asks of it. Remediation (dropping the orphan
column, resetting `alembic_version` back to `c8aa734ca08a`, deleting the
untracked migration file) was applied only after explicit user approval,
and was verified directly against the database afterward (`\d
conversation_messages`, `select version_num from alembic_version`), not
assumed successful.

**Data-loss check, closed out explicitly, not left implicit**: a specific
thread_id's checkpoint history (8 non-empty rows, from an earlier live
checkpoint-continuity test) was confirmed still present in
`checkpoint_blobs`/`checkpoints` after remediation. The schema being fixed
and no meaningful checkpoint data having been lost are two separate claims,
and both were verified rather than one being assumed to imply the other.

**Attribution**: forensic recovery of dangling git objects (`git fsck
--unreachable --no-reflog`, from a stash/pop pair) corroborates the
Task-1/2/3 subagent's own account for one specific file at one specific
timestamp. This is **not conclusively attributed, partially corroborated
for A** — it is not the same as clearing that subagent of responsibility,
and should not be read as such if anything else surfaces later.

**Fix, not just a stronger prompt**: a subagent scoped to frontend work
should never have had a path to Alembic or the database at all. Confirmed
via research (a `claude-code-guide` subagent, cross-checked directly) that
Claude Code's generic `Agent` tool has no allow/deny list for tool access,
`isolation: "worktree"` only isolates the file checkout and not DB/network
reachability, and Bash command-text deny rules in `settings.json` are
explicitly documented as not a security boundary. The only real mechanism
is a custom subagent type with a restricted `tools:` list in its
`.claude/agents/*.md` frontmatter — `frontend-worker` (added this session)
has no `Bash` tool at all, so it cannot run `npm`, touch Alembic, or reach
any database regardless of what any prompt tells it to do. Diffing
`backend/` after any subagent run, regardless of what it was scoped to do,
remains the compensating control for tasks that genuinely need Bash and
therefore can't use this narrower type.

## Status: CLAUDE.md added, domain-relevance gate (concept 44 extension)

### CLAUDE.md
Created `CLAUDE.md` at the repo root — the migration incident above was
recorded after the fact in this file, but the actual standing rule it
implies (no destructive DB/Alembic action without explicit in-turn
approval, regardless of task brief; frontend-scoped work should use the
`frontend-worker` subagent type and never touch `.py`/the database; live
LLM calls minimized) hadn't been written anywhere a future session or
subagent would automatically read. It now is. Kept short and durable —
standing rules, not task-specific instructions. `SKILLS.md` was checked
against the current commit history and was already current from the
previous documentation pass.

### Domain-relevance gate
Off-topic requests (weather, poetry, math trivia, flight booking,
chit-chat) that don't match the deterministic parser used to fall straight
through to the agent eligibility gate (`app/agent/eligibility.py`), which
only checks non-empty and under the length cap — meaning a request with
zero chance of a useful answer would still trigger a real LLM call.

Added `app/agent/domain_gate.py` (`check_domain_gate`): a small,
synchronous keyword/word-boundary screen against this system's real domain
vocabulary (derived from the parser's own grammar and the agent's tool
descriptions, not invented separately), wired into `app/api/routes/chat.py`
between the parser's unmatched branch and `check_eligibility`. Rejections
short-circuit with the existing `handled_by="rejected"` response before
`get_default_chat_model()` is ever called — proven by a test that patches
that call site and asserts it's never invoked for an off-topic request,
not just by checking the response shape.

Deliberately biased toward false negatives over false positives: only
requests sharing zero vocabulary with the domain are rejected, so anything
ambiguous-but-plausibly-hospital-related still reaches the agent. Kept as
its own component rather than folded into the regex parser, since the
parser structures known commands into a `Command` and this only screens
relevance — conflating the two would blur the "known commands are
deterministic, everything else is the agent's job" line the architecture
depends on. Documented in `docs/CONCEPT_COVERAGE.md` (concept 44,
genuinely extended, not silently folded in) and `docs/ARCHITECTURE.md`
(§1's pipeline diagram and a new rationale paragraph).

### Process for this batch
Dispatched as a single, tightly-scoped subagent (backend-only, explicit
no-DB/no-migration-of-any-kind instruction, implementation and tests
together as one unit of work) rather than split across multiple agents —
there was no clean file boundary to split along, and splitting a single
cohesive feature would only have reintroduced coordination risk for no
speed benefit. Its diff was reviewed directly (not taken on its self-
report) before committing: `git status`/diff confirmed only `backend/`
plus the two named docs changed, nothing in `frontend/`, no new migration
files, and the full backend suite (116 passed, 4 skipped) plus ruff were
re-run independently rather than trusting the subagent's own reported
numbers. Two commits: `CLAUDE.md` on its own (a process artifact, not part
of the feature), then the domain gate feature (implementation, tests, and
both doc updates together, since they're one coherent unit).

## Status: Domain gate hardening — a real bypass found and closed (concept 44b)

### The bypass
The domain gate added last session rejected a request only if *no* domain
vocabulary word appeared *anywhere* in the message — pure substring/word
presence, no requirement that the word have anything to do with the actual
request. That meant `"What's 47 times 12? mri"` sailed straight through:
the substantive request ("what's 47 times 12") is complete, off-topic
arithmetic on its own, already ended with a "?", and "mri" is a disconnected
word tacked on afterward — but because "mri" is in the vocabulary, the whole
message passed the gate and would have constructed a real LLM client to do
arbitrary off-topic work. Same shape as `"Write me a poem. patient"` or
`"Book a flight to Chicago. appointment"`: a throwaway domain word appended
to an already-complete, unrelated sentence, exploiting the fact that the
gate only ever asked "is a domain word present anywhere", never "is a
domain word part of what's actually being asked".

### The two-layer fix
1. **Gate tightening** (`app/agent/domain_gate.py`): moved from
   presence-based to intent-shape-based matching. The old single vocabulary
   set was split into `_DOMAIN_NOUNS` (entities/statuses/modalities/
   department & role names) and `_ACTION_WORDS` (the verbs/question words
   already implicit in the parser grammar and tool surface, plus ordinary
   question words like what/how/who/is/are/any). The message is split into
   clause fragments on sentence punctuation (`.`/`?`/`!`) and the
   coordinating conjunctions "and"/"but"; a request passes only if at least
   one fragment contains *both* a domain noun and an action word —
   structural co-occurrence in the same clause, not presence anywhere in
   the message. Still pure regex/set-membership, no NLP parse, no ML, no
   new dependencies — same spirit as the original gate, just no longer
   fooled by a disconnected trailing keyword.
2. **System prompt refusal instruction** (`app/agent/graph.py`'s
   `SYSTEM_PROMPT`) as defense in depth: even if a bundled off-topic request
   reaches the agent despite the gate, the agent is now explicitly told to
   answer only the hospital-relevant part (if any) of a bundled request and
   decline the rest — no arithmetic, no creative writing, no general
   knowledge, and explicitly *not* to do it anyway "to be friendly" or
   helpful. This holds independently of the gate, for whatever phrasing the
   gate's simple rules don't catch.

### Verification, no live API call
- `test_domain_gate.py`: 8 new regression fixtures reproducing the exact
  bypass pattern (including the literal `"What's 47 times 12? mri"` case)
  now correctly assert `in_domain is False`. Every pre-existing fixture —
  all 10 off-topic requests and all 7 ambiguous-but-in-domain requests —
  was re-run and still passes exactly as before; none of them relied on the
  bug (all of them either have zero domain vocabulary at all, or have their
  domain noun and action word genuinely in the same clause).
- `test_chat_api.py::test_bundled_off_topic_request_is_rejected_without_ever_invoking_the_llm`:
  the same bypass text rejected at the API level, with
  `get_default_chat_model` patched and asserted never called.
- `test_agent_loop.py`: one test asserts the refusal instruction's presence
  directly in `SYSTEM_PROMPT` text (the precise way to test "the instruction
  is there"); a second, scripted-model test proves the agent-loop plumbing
  doesn't strip or override a decline-style reply, and that the system
  message actually handed to the model matches `SYSTEM_PROMPT` verbatim —
  not just that the instruction exists somewhere in source.
- Full backend suite: **131 tests total (127 passed, 4 skipped-pending-
  credentials, unchanged from before this batch), `ruff` clean.** No
  Alembic migration, DDL, or live LLM call of any kind was run to verify
  this — everything above is offline and mock/fixture-based, per
  `CLAUDE.md`'s standing rule on minimizing live API usage.

## Status: Context-aware follow-ups and documentation synchronization

### Context-aware domain routing

The domain gate originally evaluated only the current message. That correctly
blocked off-topic input, but it also rejected normal multi-turn replies such
as `yes`, `What did you just change?`, `Why?`, and `Do that again` before the
stateful agent could see them.

`check_domain_gate()` now accepts a deliberately narrow contextual follow-up
grammar only when one of the six most recent prior user messages independently
contains connected hospital intent. `POST /api/chat` supplies user messages
only; canned assistant rejection text cannot create valid context. Explicitly
off-topic and disconnected-keyword bypass requests remain rejected.

Verification used no live model calls: focused gate tests, a mocked HTTP
routing regression, and the full `not live_llm` backend suite. Result: **140
passed, 4 live-model tests deselected**, with Ruff clean.

### Current documentation

- `rulebook.md` is now tracked as the concise command/tool/grounding/manual
  testing reference.
- `AGENT.md` documents the complete runtime agent contract: activation,
  domain, parser and command boundaries, all seven tools, grounding,
  LangGraph execution, memory, safety, observability, and offline testing.
- `docs/ARCHITECTURE.md` was reconciled with the implementation, including
  the contextual domain gate, exact registered tools, eleven application
  tables, polling-based trace transport, graph state, and current bounds.
- `SKILLS.md` now records all five subagent runs: four coding and one
  research-only.

## Status: Jev-assisted fast path merged and enabled

Merged to `master` and enabled by default (`ENABLE_JEV_FAST_PATH=true`). It
remains feature-switchable, and any decline or integration failure preserves
the existing fallback to the LangGraph agent.

### Where this came from
A prior audit evaluated TypeSafe AI's Jev — a "System One" model returning
typed, calibrated decisions instead of text — for this codebase. Its
conclusion was mostly negative: the one place with the right *shape* (the
domain gate) was the worst place to put it, because that gate exists
specifically to avoid paid calls, and replacing free regex with a paid
call there is self-defeating. Everything else was either too trivial, a
hard security boundary that must not be probabilistic (grounding), or the
one genuinely chat-shaped Sonnet call that Jev's own docs rule out.

What the audit *did* surface was a better fit one step later in the
pipeline: **model routing**. The regex parser is deliberately strict, so
plenty of ordinary phrasings ("can you pull up the department list?") miss
it and cost a full Sonnet turn to answer something the deterministic path
could have handled. Asking a cheap typed `Choice` "which known command is
this, if any?" widens that entrance without loosening the grammar itself.
The implementation uses that routing placement, not the domain-gate idea the
audit rejected.

### What it does
When the regex parser returns UNKNOWN, Jev is asked which known command
the message maps to. A confident match (project default threshold 0.9)
executes through the **same
`CommandRunner`** the regex path uses. Anything else — unconfident, an
explicit "none", an unsupported command, or any failure — falls through to
the agent exactly as before.

Placement is the safety-critical detail: the fast path sits **after** the
domain gate and eligibility gate and **immediately before** the agent. So
an off-topic message is still rejected for free rather than spending a Jev
call on it, and the only thing the fast path can ever displace is a full
Sonnet turn. It widens the deterministic entrance; it never weakens a
gate, and two e2e tests assert both gates still run first.

Arguments are never fabricated: filter answers (modality, scanner status)
come from extra `Choice` questions in the same single call, and are applied
only when they clear the same confidence threshold. An unconfident filter
is *omitted*, which is safe by construction — an unfiltered list is wider
but still correct, where a guessed filter would confidently return the
wrong answer. `search_patients` is recognised but never executed, since a
`Choice` cannot produce a free-text query.

A new `jev_invoked` trace event is written in all three outcomes (match,
decline, failure), so the trace panel shows the consultation even when it
changed nothing. `GET /api/cost-comparison` aggregates those events into a
with-vs-without tally, and a new UI page renders it.

### No schema change was needed
`AgentEvent.event_type` is already a free `String(60)`, `arguments_json` a
nullable `JSON` column, `latency_ms` a nullable `Integer` — the new event
fits the existing table exactly. Alembic is untouched, and that was
verified rather than assumed, given this project's earlier migration
incident.

### Process
Two subagents in parallel (backend, and the restricted `frontend-worker`
type for the UI). Parallel was only safe because the contract between them
— the `handled_by` value, the exact `jev_invoked` event shape, the cost
endpoint's response — was researched and frozen by the lead session *before*
dispatch and written verbatim into both briefs, rather than left for two
agents to negotiate across a boundary neither could see. See `SKILLS.md`.

Both diffs were reviewed rather than trusted, and each review found a real
bug the subagent's own tests missed:
- **Frontend**: the trace panel rendered "matched &lt;command&gt;" whenever
  Jev's answer named a command, including below-threshold answers where the
  turn actually went on to the agent — claiming an execution that never
  happened, in the one component whose job is accurate reporting.
- **Backend**: `jev_malformed_response` was missing from the failure-reason
  set, so a response shaped differently from the documented contract would
  trace as a *decline* with no error category — reading as "Jev wasn't
  confident" when the integration is in fact broken. That is the single most
  likely failure on first contact with a real key, given the SDK is a week
  old.

Both fixed with tests covering the previously-unexercised cases.

### Honest limits
The vendor launched 2026-09-15, so the integration remains isolated and
fail-open-to-agent. Version 0.7.1 was verified with a live call and the SDK is
bounded to `>=0.7.1,<1`; normal tests still use a fake SDK and consume no
credits. The absolute
saving at this project's scale is small — the originating audit said so,
and the cost page is built to show that honestly rather than flatter the
feature.

## Status: domain gate false positives fixed

The gate had drifted too strict. A probe of 28 ordinary hospital phrasings
found **17 wrongly rejected**, while all 14 off-topic probes were correctly
blocked — so the failure was entirely false positives, which the gate's own
docstring names as the costlier kind (a rejected hospital request gets a
canned refusal instead of an answer). It surfaced during manual testing on
the `jev-testing` branch, but the gate is identical on `master`, so the fix
was made on a branch off `master`.

Three causes, all in `backend/app/agent/domain_gate.py`:
1. **Entity codes were invisible.** Tokenizing turned `APT-2001` into `apt`,
   so `reschedule APT-2001 to SCN-1` held no domain word and was refused —
   the system's only write operation, phrased the most direct way, never
   reached the agent. Codes in the seeded formats are now swapped for a
   placeholder domain token before tokenizing, and count as a domain noun —
   still requiring a request word in the same clause. A looser first
   attempt ("a code beside *any* word counts") was dropped after a
   side-by-side safety probe of old vs new gate showed it let `delete
   APT-2001`, `cancel APT-2001` and `mark SCN-4 as available` through.
2. **Ordinary request verbs were missing** — `tell`, `give`, `get`,
   `describe`, `compare`, `look`, `help` and others — plus `anything`
   (only `any` was listed).
3. Fixing (2) alone would have reopened the bypass, so **commas and
   semicolons now split clauses** (`Tell me a joke, scanner` stays rejected),
   and a clause containing an unmistakably creative word (`joke`, `poem`,
   `haiku`, ...) never counts as the in-domain clause. Without that guard,
   the new verbs let `tell me a joke about patients` through — a regression
   caught by probing what the fix loosened, not only what it repaired.

### Safety check: what the fix lets through that the old gate didn't
The gate only decides whether a model is asked; it grants nothing. The fix
touches no safety layer — grounding, `CommandRunner`, the tool list, the agent
graph, the parser and the eligibility gate are all unchanged (verified by
`git diff --name-only`). Beyond that, 21 risky messages (SQL, deletes,
prompt injection, fabricated codes, credential and data-export asks) were run
through the old and new gate side by side. Only two newly reach the agent:
- `reschedule APT-9999 to SCN-1` — deliberately: it is a reschedule request,
  and grounding refuses the fabricated codes, as it already did for the worded
  form `Move appointment APT-9999 to scanner SCN-9999`.
- `show me PT-1001's social security number` — the patient schema holds only
  code, MRN, name and date of birth (the same fields `show patient <name>`
  returns deterministically), so there is nothing further to expose.

And one pre-existing gap closed: `run SQL: DELETE FROM appointments ...`
passed the *old* gate and is now rejected. Seven unserviceable/unsafe requests
are pinned as `adversarial` tests so a future change can't quietly reopen them.

Result: every probe message routes as intended. 167+ tests pass, ruff clean.
No model call was needed to verify any of it.

Honest limit: `what is 47 times 12 for the MRI appointment?` passes, as it did
before this change — "times" is also a scheduling word, and a keyword gate
cannot read intent. The system prompt's refusal instruction is what declines
it, at the cost of one model call.

## Status: `voice` branch — streaming voice input, Phases 1-2 (experimental)

Not merged to `master`. A pre-build architecture audit (see git history)
evaluated transport, VAD, and STT choices against this project's actual
code and current external pricing before any implementation; this phase
implements what that audit recommended. Full reasoning lives in
`docs/voice.md`, kept current as each phase lands rather than written
after the fact.

**Phase 1** extracted `chat.py`'s inlined routing into
`handle_chat_message()`, a plain function with no HTTP types in its
signature; `POST /api/chat` is now a thin wrapper around it. This is the
convergence point voice and text both go through — the same
one-trusted-implementation discipline this project already applies to
`CommandRunner`. Verified as a true no-op: the offline suite's pass count
was identical before and after (216 passed, 6 skipped both times), which
is itself the proof nothing behavioral moved.

**Phase 2** added local `faster-whisper` STT (`app/voice/stt.py`) and
proved the full chain — audio file, real transcription, real routing, real
`ChatResponse` — before any real-time code exists. Model choice (`tiny.en`
over `base.en`) was measured against a checked-in synthesized fixture
(`tests/fixtures/audio/list_delayed_mri_appointments.wav`), not assumed:
~450ms transcription warm, no accuracy loss on short command audio.
`faster-whisper`'s own dependency metadata was checked before adding it —
no torch in the base install, consistent with the rest of this stack.

The new test (`test_voice_stt_pipeline.py`) runs real local transcription
(free, no live call) against the fixture, then posts the transcript
through the real `/api/chat` route with Jev and the agent mocked —
covering both branches of the target architecture's UNKNOWN path: a
confident Jev match executing through the real `CommandRunner`, and a Jev
decline reaching a scripted agent. 218 passed (216 + 2), 6 skipped, ruff
clean; no live API call was made to verify any of it.

**Phase 3** added the WebSocket endpoint itself
(`app/api/routes/voice.py`) and the energy-based RMS VAD state machine
(`app/voice/vad.py`) — chunk-level and duration-based, not wall-clock, so
a test can stream chunks as fast as it wants and still get deterministic
transitions. All three flagged failure modes from the pre-build audit are
handled explicitly: an empty/blank transcript never reaches
`handle_chat_message`; an STT exception degrades to `error: stt_failed`
without killing the connection; a connection dropped genuinely
mid-utterance discards the buffer and records a `voice_connection_dropped`
trace event (status `FAILURE`, `arguments.buffered_ms`) so that loss isn't
silent, while a drop while idle records nothing, since nothing was lost.
The max-utterance-duration cutoff is explicitly *not* treated as a
failure — same transcription and routing, only `speech_ended`'s `reason`
differs.

One existing file needed a small, deliberate change: `chat.py`'s
`get_checkpointer` is now typed `HTTPConnection` instead of `Request`.
This wasn't a style choice — verified directly against FastAPI's own
dependency resolution that the *unmodified* `Request`-typed version raises
`missing 1 required positional argument` under a WebSocket route, since a
`Request`-typed `Depends()` parameter is only ever populated for an actual
`Request` instance. `HTTPConnection` is the base both `Request` and
`WebSocket` share, so the fix is type-only; the function body and every
existing HTTP-side override are unchanged.

RMS thresholds were measured against this project's own fixture audio
(silent gaps ~0.0 normalized RMS, in-speech windows ~0.04-0.24), not
picked blind — `voice_vad_speech_rms_threshold=0.02` sits above the noise
floor and below typical speech energy, with an explicit note in
`config.py` that, unlike `voice_stt_model`, there is no universally
"correct" value here.

Testing this required solving a real problem, not just writing assertions:
`TestClient.websocket_connect()` runs the app inside its own background
thread with its own event loop, and an async engine built inside
pytest-asyncio's loop (every other fixture's pattern) fails with asyncpg's
"attached to a different loop" error when reused there — reproduced
directly against this project's own Postgres before working around it,
not assumed. `test_voice_websocket.py` uses plain sync test functions with
a lazily-constructed, per-test DB engine instead. A second harness quirk
(the test client cancels its portal task shortly after disconnecting,
without waiting for the server's cleanup coroutine to finish) meant the
dropped-connection test polls for its DB row explicitly rather than
trusting the client's own teardown timing. Both are documented in
`test_voice_websocket.py`'s module docstring and `docs/voice.md`'s Testing
section.

Result: 223 passed (218 + 5 new WebSocket tests), 6 skipped, ruff clean. No
live LLM/Jev API call, no Alembic or schema-affecting command, and no file
outside `backend/` (plus these two docs) was touched to build or verify
any of it.

## Status: `voice` branch — Phase 4, frontend mic control (experimental)

Dispatched to the restricted `frontend-worker` subagent type (no `Bash`)
against Phase 3's WS message contract, frozen and reviewed before dispatch.
A mic button in `ChatPanel` streams captured mic audio over
`/api/voice/{session_id}` and renders the resulting turn through the exact
same message list and badge rendering a typed turn already uses — no new
UI language for voice. Full writeup in `docs/voice.md`'s Phase 4 section.

Reviewing the diff (not just trusting the report) found one real
correctness bug and fixed it directly: the audio downsampling used
nearest-integer-ratio decimation, which is only actually correct when the
browser's native sample rate happens to be an exact multiple of 16000. At
the very common 44100Hz native rate, the rounded ratio (3) silently
produces audio at an effective 14700Hz mislabeled as 16000Hz -- an 8.8%
speed/pitch distortion into Whisper, confirmed by computing the actual
effective rate for common browser sample rates rather than assumed.
Replaced with continuous-phase linear interpolation resampling -- still a
deliberately simple technique, not a full resampling filter, but correct
for any source rate rather than only exact multiples of 16000. Also added
a small fix so an unsupported-browser capture failure surfaces as an
explicit error instead of leaving the UI silently stuck at "Listening..."
forever with no audio ever sent.

Running the tests independently (not just trusting "should pass") also
surfaced two real test failures the subagent's own report didn't catch --
both a test-assertion problem, not a production bug: the "first message of
a new session" bookkeeping (pre-existing, unrelated to voice) writes the
same text into the "Previous chats" preview list, so an unscoped text query
matched two elements. Fixed by scoping the queries to the actual message
bubbles; one of the two also needed reading bubble `textContent` directly
rather than `getByText`, since the assistant bubble renders through
`ReactMarkdown`'s nested `<p>` while the user bubble is plain text -- an
asymmetry a single selector-scoped query couldn't handle uniformly.

Full suite after fixes: 33 passed, build clean, lint clean (only the same
pre-existing `set-state-in-effect` warnings already present before this
phase). Honest limit, stated plainly rather than implied as covered: real
browser mic permission prompts, actual audio quality against the live
backend, and that "stop" genuinely releases the mic-in-use indicator are
real-hardware/real-browser behavior no headless review or jsdom test can
exercise -- verified by code inspection and what automation can cover, not
by hand in an actual browser yet.

All four phases of the `voice` branch are now complete. Not merged to
`master`; left for review per the build's own instruction.

## Status: `voice` branch — first live test matrix, and the parser bug it exposed

A 16-command spoken test matrix (real Chromium, fake mic fed synthesized
audio, real app, nothing mocked) was run purely to *list* problems before
fixing any. Full table in `docs/voice.md`. The finding that mattered most was
not a mishearing: **trailing punctuation broke the deterministic parser.**

Whisper appended `.`/`?` to 13 of the 16 transcripts, and most grammar rules
are `$`-anchored, so they missed and took a paid Jev detour. Worse,
`show patient <name>` has no anchor: `show patient David Davis.` matched,
searched for the literal `"David Davis."`, and returned **"No patients matched
that search."** under a `DETERMINISTIC` badge — a confidently wrong empty result
with no error anywhere. Two of the four commands that passed deterministically
in the matrix only did so because Whisper happened to omit the period that run.
It is not voice-specific: reproduced *before* the fix by typing the same three
messages into the real UI.

Fixed in `parse()` — the one function typed chat, spoken chat, `/api/commands`
and the agent's `execute_command` all share — by collapsing whitespace and
dropping trailing sentence punctuation; punctuation inside a name is kept and
the grammar is no fuzzier than before. 112 new test cases; verified they
*fail* against the old parser (53 failures) before trusting them. Suite: 335
passed, 6 skipped, ruff clean.

Correction to an earlier claim: the matrix report said 9 of 16 transcripts
carried terminal punctuation. The actual count is 13 of 16; the mistake
understated the problem and was caught while writing this entry.

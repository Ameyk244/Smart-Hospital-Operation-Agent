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

### Next
Phase 5: LLM provider abstraction (Anthropic default + OpenRouter
alternative) with a `FakeChatModel` test double, then the tool registry and
first real structured tool, then the LangGraph loop itself.

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

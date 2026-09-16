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

### Next
Phase 3: deterministic parser + `Command` grammar + `CommandRunner` +
execution functions (reschedule/assign scanner, list/search reads),
fully tested without any LLM/agent code involved.

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

# Standing rules for this project

Smart Hospital Operations Agent — a modular monolith. `backend/`
(Python/FastAPI/PostgreSQL/LangGraph) and `frontend/` (React/TypeScript/
Vite) are separate concerns. See `docs/ARCHITECTURE.md` for the full
design, `AGENT.md` for the runtime agent contract, `JEV.md` for the read-only
fast path, `docs/PROGRESS.md` for
the implementation timeline, and `rulebook.md` for the current command,
tool, routing, grounding, and manual verification reference.

## Jev read-only fast path
The main request flow includes TypeSafe AI's Jev (`typesafe-sdk`) and a
`TYPESAFE_API_KEY` env var, used to widen the
deterministic fast path: when the regex parser returns UNKNOWN, Jev is asked
which known command (if any) the message maps to, and a confident match is
routed through the existing `CommandRunner` instead of a full Sonnet agent
turn. It is enabled by default (`ENABLE_JEV_FAST_PATH=true`) but remains
operator-configurable and must fail through to the agent when unavailable.

The Jev path must remain after the domain and eligibility gates, stay
read-only, and never bypass or weaken grounding or another safety mechanism.
It only hands off to the same trusted `CommandRunner` an exact regex match
would have used.

## Destructive/DB actions require explicit approval, every time
No subagent or session may run Alembic migrations, `DROP`/`ALTER`
statements, or any other schema-affecting command against the dev
database without explicit human approval **in that turn** — regardless of
what its task brief says or how safe it looks. This rule exists because a
subagent scoped to frontend-only work once ran an unrelated Alembic
migration against the live dev database anyway, dropping LangGraph's own
checkpoint tables as a side effect (see `docs/PROGRESS.md`'s incident
writeup). A task brief saying "frontend only" is not an enforced boundary
by itself — treat it as advisory, not a guarantee.

## Scoped tool access per subagent role
Frontend-only work should be dispatched to the `frontend-worker` subagent
type (`.claude/agents/frontend-worker.md`), which has no `Bash` tool at
all — the only real enforcement mechanism Claude Code supports for this,
since prompt-level scoping alone isn't a boundary. Frontend-scoped work
should never touch `.py` files or the database. Regardless of which
subagent type is used, diff `backend/` after any subagent run as a
compensating control, even when the task was never supposed to touch it.

## Live API usage
Live calls to the real LLM should be minimized. Never use a live call for
routine verification when a mocked/fixture-based test would prove the same
thing. Prefer scripted/fake-model tests for agent-loop behavior; reserve
live calls for genuine end-to-end sanity checks, and keep those to the
minimum number actually needed.

## Architecture invariants worth knowing before touching the request path
- Deterministic-first: every request tries the regex command parser
  (`app/parser/parser.py`) before ever considering the LLM. The parser is a
  small, fixed grammar — not a general NLU layer — by design.
- `CommandRunner` is the single trusted execution chokepoint shared by both
  the deterministic parser path and the agent's tool-calling path. Don't
  duplicate execution logic outside it.
- Grounding-by-rejection: entity codes (appointment/scanner/patient) must
  be exposed to a session via a search/lookup before a write tool can
  reference them. Grounding is scoped per session.
- `reschedule_appointment` is the only tool in the whole system that
  mutates hospital data. Everything else is either read-only or writes to
  session-scoped preference memory, not the hospital schema.

## Process norms
- Commit only when explicitly asked; never `git add -A`/`.` blindly —
  stage explicit file lists.
- Keep `rulebook.md` tracked and update it when parser commands, agent tools,
  read/write boundaries, routing, grounding, or seed reference data change.
- One commit per coherent unit of work, with a message explaining why, not
  just what.
- Update `docs/PROGRESS.md` (and `SKILLS.md` for delegation patterns) as
  work happens, not reconstructed after the fact.

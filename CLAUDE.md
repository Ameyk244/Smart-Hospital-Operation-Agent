# Standing rules for this project

Smart Hospital Operations Agent — a modular monolith. `backend/`
(Python/FastAPI/PostgreSQL/LangGraph) and `frontend/` (React/TypeScript/
Vite) are separate concerns. See `docs/ARCHITECTURE.md` for the full
design, `AGENT.md` for the runtime agent contract, `JEV.md` for the read-only
fast path, `docs/voice.md` for the streaming voice input layer (design, measurements, test findings),
`docs/PROGRESS.md` for
the implementation timeline, `rulebook.md` for the current command,
tool, routing, grounding, and manual verification reference, and
`docs/harness-smart-hospital-operations.md` for the harness runtime (design,
contract, what changed and why).

## Voice input (merged from the `voice` branch)
Streaming voice input runs alongside the text pipeline: a WebSocket transport
(`app/api/routes/voice.py`), local `faster-whisper` speech-to-text (CPU, no GPU
dependency), and energy-based (RMS) VAD with a short pre-roll for utterance
endpointing -- deliberately not Silero VAD, since its packaged pip install pulls
in `torch`/`torchaudio`, a genuinely heavy addition nothing else in this stack
needs; that trade is documented in `docs/voice.md`, not decided silently. It
requires **no new paid API or secret** -- local STT only.

Voice and text converge into the exact same code path with no duplicated
routing logic: both call the same `handle_chat_message()` in
`app/api/routes/chat.py`, the way the parser and agent tools already share one
`CommandRunner`. A voice session reuses the app's existing `session_id` as the
LangGraph `thread_id` exactly as the text path does -- there is no separate
voice-session concept at the checkpointing layer. Anything that changes what
text reaches routing for voice (the vocabulary correction in
`app/voice/vocab.py`) lives in the voice layer, is voice-only, and stays a short
explicit table, not a model. Turns answered by the parser or Jev are recorded in
the agent's checkpoint (`record_non_agent_turn`) so follow-ups see them.

Rules from prior incidents still hold: no destructive DB action without
explicit in-turn approval regardless of task brief, subagents stay scoped to
the files they were assigned, and every subagent's diff is reviewed -- with
`git status`/diff checked against its intended scope -- before it's committed.

## Observability (branch `observability`)
Everything for this work lives under the top-level `observability/` folder
(`README.md` is the master reference, `CONCEPT_MAP.md` maps the 10-module
curriculum to the code, `dashboards/` holds the checked-in Grafana JSON,
`docker-compose.yml` runs the stack). It adds the OpenTelemetry SDK
(metrics + traces), a self-hosted `grafana/otel-lgtm` container, PHI/secret
redaction on the existing logging/tracing, and an audit record for the one
mutating tool. It requires **no new paid API or secret** -- every piece is
local, self-hosted tooling.

- **Never in the hospital UI.** No frontend link, button, or page references
  Grafana or any telemetry endpoint, now or in a future deployment. Grafana
  is opened directly at `localhost:3001` (not 3000: that port is taken by
  another local project; override with `GRAFANA_PORT`).
- **Telemetry is optional at runtime.** The app and the offline test suite
  must work with the collector down or absent; exporting failures never fail
  a request.
- **Redaction is fail-closed.** `app/observability/redaction.py` allowlists
  what may be stored in `agent_events.arguments_json` and logs; anything not
  on the list (free text such as `command_text`, patient-search queries,
  preference values) is replaced, not passed through.
- **Cardinality rule.** Metric labels are low-cardinality only (`route`,
  `tool_name`, `status`, `error_category`, ...). Never `session_id`,
  `request_id`, or any entity/patient code as a metric label -- those belong
  on spans and log lines.
- The existing `agent_events` table and trace panel stay exactly as they are;
  OTel complements them.

## Jev read-only fast path
The main request flow includes TypeSafe AI's Jev (`typesafe-sdk`) and a
`TYPESAFE_API_KEY` env var, used to widen the
deterministic fast path: when the regex parser returns UNKNOWN, Jev is asked
which known command (if any) the message maps to, and a confident match is
routed through the existing `CommandRunner` instead of a full Sonnet agent
turn. It is enabled by default (`ENABLE_JEV_FAST_PATH=true`) but remains
operator-configurable and must fail through to the agent when unavailable.

The Jev command path must remain after the domain and eligibility gates, stay
read-only, and never bypass or weaken grounding or another safety mechanism.
It only hands off to the same trusted `CommandRunner` an exact regex match
would have used.

Jev has a second, separate use: a **scope verdict** for a message the keyword
gate cannot place (`classify_scope`, AGENT.md section 3). It runs after the deny
layer and the eligibility gate, can only ever *reject* (a confident
"not_hospital"; no verdict also rejects, i.e. fail closed) or let the message on to
the agent, and never executes anything. This deliberately replaces "the keyword
gate rejects what it cannot place", which blocked legitimate follow-ups; the deny
layer and the agent's prompt, tools and output guard stay in force. Any change
here needs a measurement against a labelled message set, not intuition.

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
- Three tools mutate hospital data: `reschedule_appointment`, `assign_bed`
  and `release_bed` (branch `hospital-expansion`, `docs/EXPANSION.md`). All
  three are grounded and audited through `CommandRunner`. Everything else is
  either read-only or writes to session-scoped preference memory, not the
  hospital schema.

## Harness runtime (branch `harness-smartgateway`)
The agent's model loop can run in a separate harness process (a plugin on a
fork of DeepSeek Harness) while all tool rules stay here. The full design and
the **[contract]** sections live in `docs/harness-smart-hospital-operations.md`;
the pattern reference is `docs/harness-refrence.md`. Two parts: Part 1 is this
repo (`ToolTurn` extraction, `/internal/v1` route, bridge, runtime switch);
Part 2 is the fork's `hospital-gateway/` folder, built only after Part 1 is
verified. It needs **no new paid API or secret** (a local shared token only).

- `AGENT_RUNTIME=in_process` stays the default and must behave exactly as
  before. The harness runtime may never weaken a gate: it sits where
  `run_agent` sits, after the parser, domain gate, eligibility gate and Jev.
- `ToolTurn` (`app/agent/turn.py`) is the one place tool-call rules live
  (validation, grounding, caps, timeout, audit, events). LangGraph's tool node
  and the internal route both call it; never duplicate those rules elsewhere.
- Change the contract by editing the doc first, then the code and fixtures.
- No schema change is part of this work. Any migration or DDL still needs
  explicit approval in that turn.
- Harness logs and spans carry ids and enums only; provider or harness error
  text is never shown to the user.
- Part 2 lives in a **separate repo**: the public fork
  `github.com/Ameyk244/harness-smart-agent`, cloned at
  `C:\dev\harness-smart-agent` (outside OneDrive: pnpm's absolute-path
  junctions break if the folder is renamed or moved after install), branch
  `hospital-gateway` on upstream tag `dsh-v0.2.1-alpha.2`. **Never edit dsh's
  own source**; all gateway code goes under `hospital-gateway/`. Design: section
  11 of the harness doc. Subagents working there still never run anything
  destructive against the dev database, and every subagent diff is reviewed
  (`git status`/diff in both repos) before it is committed. The gateway's
  tests use a mock LLM; the single live smoke is the only paid call.

### Harness extensions (branch `harness-ext-test`)
Five optional features sit on top of the harness (see `docs/PROGRESS.md`). The rule
for all of them: **additive or opt-in, off by default**, so a fresh clone behaves
exactly as before. The switches are `HOSPITAL_LOCAL_TOOLS` and
`HOSPITAL_LLM_FALLBACK_MODELS` (gateway), `HARNESS_FALLBACK_MODEL` and
`REQUIRE_RESCHEDULE_CONFIRMATION` (backend). Gateway-local tools must be harmless by
construction (no hospital data, no I/O); anything touching hospital data goes through
the backend. **Never run two database test runs at once**: they truncate each
other's rows. Pushing and merging need an explicit instruction.

A sixth, opt-in add-on (branch `harness-front-door`): `HOSPITAL_FRONT_DOOR=true` makes
the gateway the browser's entry point, relaying `/api/*` and the voice socket to the
backend unchanged (design doc section 11.8). It is a transparent relay: the parser,
Jev, gates, grounding and output guard all stay in the backend and nothing there may
move into the gateway. Run modes: 1 = `in_process`, no gateway; 2 = `harness`, front door
off (parser and Jev work with the gateway down); 3 = `harness` plus front door (the gateway
is the browser's only entry and must stay up for everything). See
`docs/HARNESS_EXPLAINED.md`, "The three run modes".

### How to finish a unit of harness work (standing instruction to myself)
1. After Part 1's code is in, **start the app** (backend in `harness` mode
   against a stub harness, and in `in_process` mode) and confirm a real request
   works. A passing test suite alone is not "done".
2. Then run a **code review** (`/code-review`) over the whole branch diff and
   fix what it finds.
3. **Production-level code only**: the feature must work properly, with no
   unnecessary lines. No speculative options, dead code, defensive branches for
   states that cannot happen, re-explaining comments or wrappers that add
   nothing. Match the surrounding comment density and idiom. When in doubt,
   leave it out and note it in the doc's "not built" list.
4. Report progress as work happens (what is done, what is next), and update
   `docs/PROGRESS.md` and `SKILLS.md` as it happens.

## Hospital expansion (branch `hospital-expansion`)
Wards and beds, the emergency queue and the patient journey, built on the
same rails as everything else: new parser commands, agent tools, gate
vocabulary and Jev read commands, with the agent still one global agent (never
page-specific). Design, schema and the HTTP contract are in
`docs/EXPANSION.md`. Jev may only name the new *read* commands; the two bed
writes are never reachable from it. The UI is read-only (changes go through
chat) and navigation is a left sidebar over the existing `view` switch, with no
router. Adds three tables, so it needs an Alembic migration that, like any DDL,
needs explicit approval in that turn.
`REQUIRE_BED_CONFIRMATION` (opt-in, default off) makes the agent's `assign_bed` /
`release_bed` only propose a change until the user confirms it in the chat
(`docs/EXPANSION.md` section 7a); it reuses `agent_events`, with no schema change.

## Process norms
- Commit only when explicitly asked; never `git add -A`/`.` blindly —
  stage explicit file lists.
- Keep `rulebook.md` tracked and update it when parser commands, agent tools,
  read/write boundaries, routing, grounding, or seed reference data change.
- One commit per coherent unit of work, with a message explaining why, not
  just what.
- Update `docs/PROGRESS.md` (and `SKILLS.md` for delegation patterns) as
  work happens, not reconstructed after the fact.

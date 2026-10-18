# How the harness works: a plain-language guide

Last checked against the backend repo at commit `34a9d8e` and the gateway repo at
commit `e247035`. This guide explains *how it works and where everything is*. The
formal contract and the design decisions are in
[harness-smart-hospital-operations.md](harness-smart-hospital-operations.md); the
pattern it was modelled on is in [harness-refrence.md](harness-refrence.md).

## Contents

1. [The idea in one minute](#1-the-idea-in-one-minute)
2. [The four places involved](#2-the-four-places-involved)
3. [Which messages use the harness](#3-which-messages-use-the-harness)
4. [The diagram: one agent turn, step by step](#4-the-diagram-one-agent-turn-step-by-step)
5. [The same journey for every kind of message](#5-the-same-journey-for-every-kind-of-message)
6. [The two calls between backend and gateway](#6-the-two-calls-between-backend-and-gateway)
7. [Who protects what](#7-who-protects-what)
8. [Files in this repo (Smart Hospital Operations Agent)](#8-files-in-this-repo)
9. [Files in the gateway repo (hospital-gateway-private)](#9-files-in-the-gateway-repo)
10. [What we use from DeepSeek Harness, and what we don't](#10-what-we-use-from-deepseek-harness)
11. [Settings and how to run it](#11-settings-and-how-to-run-it)
12. [What can go wrong, and what the user sees](#12-what-can-go-wrong)
13. [Known limits](#13-known-limits)

---

## 1. The idea in one minute

Before, the backend did everything for an agent turn: it called the language model,
kept count of rounds, ran the tools, and applied the hospital rules.

Now the **model loop** (call the model, read its answer, see whether it wants a tool,
repeat) can run in a separate program, the **harness**. The backend keeps everything
that decides what a tool call actually *does*: checking the arguments, checking the
entity codes were seen this session, running the real database action, counting
limits, writing the audit and trace rows.

The harness never runs a hospital tool itself. Every time the model asks for a tool,
the harness sends the request back to the backend and waits for the answer. So the
hospital rules live in exactly one place, and both ways of running the agent obey
them.

Switch: `AGENT_RUNTIME=in_process` (the default, the original behaviour) or
`AGENT_RUNTIME=harness`.

## 2. The four places involved

| Place | What it is | Where the code lives |
|---|---|---|
| **BACKEND** | Your FastAPI app. Owns the hospital rules and the data | this repo, `backend/app/` |
| **GATEWAY** | Our small adapter that runs inside DeepSeek Harness | gateway repo, `hospital-gateway/src/` |
| **dsh** | DeepSeek Harness: the open-source engine that runs the model loop | the same fork repo; we never edit its code |
| **MODEL** | The real language model (Claude through OpenRouter) | the internet |

The gateway repo is a fork of DeepSeek Harness, with our one extra folder,
`hospital-gateway/`, added. It lives at `C:\dev\harness-smart-agent` and is backed up
in the private GitHub repo `hospital-gateway-private`.

## 3. Which messages use the harness

Only messages that **need the agent**. The parser and Jev answer inside the backend
and never contact the gateway, even when `AGENT_RUNTIME=harness`.

```
 Browser (typed or voice) ──> POST /api/chat            BACKEND  chat.py
                  │
                  ├─ the parser matches?   ──yes──> answered here.  Harness NOT used.
                  ├─ a gate says no?       ──yes──> rejected here.  Harness NOT used.
                  ├─ Jev is confident?     ──yes──> answered here.  Harness NOT used.
                  │
                  └─ none of those ──> the message NEEDS THE AGENT
                                          │
                                          ▼  (only if AGENT_RUNTIME=harness)
                                     STEP 1 of the diagram below
```

The check of `AGENT_RUNTIME` sits at the agent step only. That is why parser and Jev
messages stay fast and free when the gateway is down: only agent questions show
"temporarily unavailable".

## 4. The diagram: one agent turn, step by step

```
╔═ BACKEND ═══════════════════════════════════════════════════════════════╗
║ 1. backend/app/agent/harness_bridge.py     "run this turn"              ║
║      packs: system prompt + chat history + your message + the 8 tools   ║
║      registers this turn in harness_registry.py                         ║
╚═════════════════════════════════╤═══════════════════════════════════════╝
                                  │ 2. POST /v1/turns   (+ secret token)
                                  ▼
╔═ GATEWAY (inside dsh) ══════════════════════════════════════════════════╗
║ 3. gateway.ts       token ok? body size ok? fewer than 4 turns running? ║
║ 4. agent-driver.ts  builds a fresh dsh agent for this one turn          ║
║    seed.ts          loads the chat history into it                      ║
║    tool-proxy.ts    gives it a stand-in for each of the 8 tools         ║
╚═════════════════════════════════╤═══════════════════════════════════════╝
                                  │ 5. dsh loop sends the conversation
                                  ▼
╔═ MODEL ═════════════════════════════════════════════════════════════════╗
║ 6. reads it, then either:  (a) writes the final answer, or              ║
║                            (b) asks for a tool, e.g. search_appointments║
╚═════════════════════════════════╤═══════════════════════════════════════╝
                                  │ (b) a tool is requested
                                  ▼
╔═ GATEWAY ═══════════════════════════════════════════════════════════════╗
║ 7. tool-proxy.ts does NOT run the tool. It forwards it back:            ║
║    raw-args.ts keeps the model's exact argument text                    ║
╚═════════════════════════════════╤═══════════════════════════════════════╝
                                  │ 8. POST /internal/v1/turns/{id}/tool_calls
                                  ▼
╔═ BACKEND ═══════════════════════════════════════════════════════════════╗
║ 9.  internal_harness.py   checks the token FIRST, then finds the turn   ║
║ 10. turn.py  ToolTurn:   count limit → known tool? → valid arguments?   ║
║                          → grounding → run the real tool on PostgreSQL  ║
║                          → write the trace event                        ║
╚═════════════════════════════════╤═══════════════════════════════════════╝
                                  │ 11. result text goes back
                                  ▼
                  gateway hands it to the model ──> back to step 6
                  (repeats until the model gives a final answer
                   or a limit stops it: max rounds, time, tool cap)

 When the model gives the final answer (6a):
╔═ GATEWAY ═══════════════════════════════════════════════════════════════╗
║ 12. agent-driver.ts   packs the reply, token usage, and every round     ║
╚═════════════════════════════════╤═══════════════════════════════════════╝
                                  │ 13. response to the step 2 request
                                  ▼
╔═ BACKEND ═══════════════════════════════════════════════════════════════╗
║ 14. harness_bridge.py   if the backend stopped the turn, that wins      ║
║                         rebuilds the full history of the turn           ║
║                         saves it to the checkpoint                      ║
║ 15. chat.py             returns the reply to the browser                ║
╚═════════════════════════════════════════════════════════════════════════╝
```

Three rules this diagram shows:

1. **The gateway never runs a tool.** Steps 7 to 11 always bounce the call back, so
   grounding, validation, caps and audit all run in the backend.
2. **A secret token guards both doors** (steps 2 and 8). A wrong token gets an
   ordinary "404 Not Found", the same as for a path that does not exist.
3. **Steps 6 to 11 can repeat.** Each time around is one "round". The round limit and
   the time limit stop it from running forever.

## 5. The same journey for every kind of message

Everything starts in `handle_chat_message()` in
[backend/app/api/routes/chat.py](../backend/app/api/routes/chat.py). Typed messages
and voice both go through it.

| Message example | What answers it | Model calls | `handled_by` | Trace rows |
|---|---|---|---|---|
| `list scanners mri available` | The regex **parser** runs the command through `CommandRunner` | none | `deterministic` | none |
| `which MRI scanners are available?` | **Jev** maps it to a known command (confidence at least 0.9), then the same `CommandRunner` runs it | 1 Jev call, no Claude | `jev` | `jev_invoked` |
| `What is the weather today?` | The **domain gate** refuses it with a canned reply | none | `rejected` | none |
| `Which delayed MRI appointments could be moved…?` | The **agent**: in-process LangGraph, or the harness (section 4) | about 2 per tool call | `agent` | `agent_invoked`, `tool_executed` (and `llm_response` in-process) |

What each path does besides answering:

- **Parser and Jev** both append the reply to `conversation_messages` and call
  `record_non_agent_turn()` in `graph.py`, which writes the exchange into the agent's
  checkpoint, so a follow-up such as "what did you just find?" works.
- **If Jev declines** (below the threshold, no key, a timeout), a `jev_invoked` row
  marked REJECTED or FAILURE is written and the message goes on to the agent. Jev can
  never block an answer.
- **An agent write**, for example "move the first delayed CT appointment to a free CT
  scanner": round 1 searches, and every code returned becomes *grounded* for this
  session. Round 2 calls `reschedule_appointment`. `ToolTurn` validates the
  arguments, requires both codes to be grounded, and runs `reassign_scanner` through
  `CommandRunner`, which checks modality and availability and writes the change and an
  `audit_log` row in one transaction. A made-up or cross-session code is refused, the
  model sees the refusal, nothing changes, and the invalid-call counter goes up. This
  is identical in both runtimes.

## 6. The two calls between backend and gateway

Both are plain HTTP with JSON and the header `x-harness-token`.

**Backend to gateway:** `POST {HARNESS_URL}/v1/turns`

| Request field | Meaning |
|---|---|
| `turn_id` | 32 hex characters naming this turn |
| `system` | The system prompt, including the user's saved preferences |
| `history` | Earlier messages in OpenAI format (never a system message, never the new one) |
| `user` | The new message |
| `tools` | The backend's tools in registry order |
| `llm` | `model`, `max_tokens`, `deadline_in_ms`, `max_rounds` |

The response (the "finish payload") has `stop_reason` (`null`, `round_limit`,
`time_limit` or `error`), a short `error` code when it is `error`, `model_text`,
`rounds`, `usage` (prompt, completion and cached tokens) and `assistant_rounds` (each
round's text and the exact tool calls it made).

**Gateway to backend:** `POST /internal/v1/turns/{turn_id}/tool_calls`

Request: `round`, `call_id`, `name`, and `arguments` as the model's **raw** text.
Response: `content` (exactly what the model should see) and `end_turn`, which is
`null`, `max_tool_calls_exceeded` or `too_many_invalid_calls`.

The exact rules, bounds and status codes are in section 5 and 6 of
[harness-smart-hospital-operations.md](harness-smart-hospital-operations.md). The
seven example JSON files that both repos keep identical are in
`backend/tests/fixtures/harness_contract/` and
`hospital-gateway/contract/fixtures/`; `pnpm contract:sync` fails if they differ.

## 7. Who protects what

| Rule | Enforced by | File |
|---|---|---|
| Parser first, then gates, then Jev, then agent | backend | `chat.py` |
| Tool-call cap (10), invalid-call cap (3), 15 s per tool, argument validation, grounding | **backend**, for both runtimes | `turn.py` |
| Only `reschedule_appointment` writes hospital data, and every write is audited | backend | `agent/tools/`, `CommandRunner` |
| Round limit (6) and the whole-turn deadline | gateway | `agent-driver.ts` |
| The backend's stop reason beats any harness stop reason | backend | `harness_bridge.py` |
| Token checked first, in constant time, before the body is read | both | `internal_harness.py`, `gateway.ts` |
| The gateway listens on the loopback address only | gateway | `config.ts` |
| Logs hold only ids and numbers, never message text or tokens | gateway and backend | `log.ts`, backend redaction |
| Provider or harness error text is never shown to the user | backend | `harness_bridge.py` |
| A refused startup if a service that could store or export text is mounted | gateway | `plugin.ts` |

## 8. Files in this repo

Repo: **Smart Hospital Operations Agent**. Harness work touches these files.

### Request path (`backend/app/`)

| File | Role | Why it is there |
|---|---|---|
| `api/routes/chat.py` | The one routing function. At the agent step it chooses harness or in-process by `AGENT_RUNTIME` | The only switch; nothing before it changed |
| `agent/harness_bridge.py` | Runs a turn through the harness: builds the payload, calls the gateway, serves its tool calls, maps the result, rebuilds the history, saves it to the checkpoint, handles failures | The backend's half of the contract |
| `agent/harness_registry.py` | A small dictionary of turns currently running, so a tool call can find its turn | Lets the internal route find the right `ToolTurn` |
| `agent/turn.py` | `ToolTurn`: every rule for one tool call (count cap, unknown tool, validation, grounding, timeout, events, spans, metrics) | Extracted from the graph so both runtimes share one rule set |
| `api/routes/internal_harness.py` | `POST /internal/v1/turns/{id}/tool_calls`, guarded by the token | The door the gateway calls |
| `agent/graph.py` | The LangGraph loop. Its tool node now just calls `ToolTurn`. Gained `get_thread_messages`, `record_turn_messages`, and a public `build_system_message` | Shared checkpoint helpers; same behaviour as before |
| `agent/history.py` | Added `to_openai_messages`, converting history to the format the contract uses | Contract format |
| `config.py` | Settings: runtime, URL, token, model, token cap, timeout, with startup checks | Fails fast on a bad token, URL or limit |
| `main.py` | Registers the new routers | Wiring |

### Status lights

| File | Role |
|---|---|
| `backend/app/api/routes/status.py` | `GET /api/status`: reports agent, harness and Jev as `on`, `off` or `down`. Probes the gateway's `/healthz`; Jev is configuration only |
| `frontend/src/hooks/useStatus.ts` | Polls `/api/status` every 15 s |
| `frontend/src/components/StatusLights.tsx` | The three lights in the header |
| `frontend/src/App.tsx`, `App.css` | Places the lights and styles them |
| `frontend/src/api/client.ts`, `types.ts` | The API call and its types |

### Tests

| File | What it proves |
|---|---|
| `tests/integration/test_tool_turn.py` | `ToolTurn` works with no model and no graph |
| `tests/integration/test_internal_harness_route.py` | Token first, 404 for strangers, 422 for a bad body, a replayed call is not re-run |
| `tests/unit/test_harness_bridge.py` | The pure parts: history conversion, message rebuild, reason mapping, call ordering |
| `tests/unit/test_harness_contract.py` | Strict models check every example JSON file |
| `tests/integration/test_harness_world.py` | The real app, database, checkpointer, route and bridge together; only the HTTP call to the gateway is a script. Covers the happy path, history continuity, grounding, caps, failures and the runtime switch |
| `tests/unit/test_config.py` | The startup checks on the harness settings |
| `tests/integration/test_status_route.py` | Every light state, with a real local stand-in for the gateway |
| `frontend/src/components/StatusLights.test.tsx` | The component and the polling hook |

### Fixtures, docs and config

| File | Role |
|---|---|
| `backend/tests/fixtures/harness_contract/*.json` (7 files) | The shared example messages, byte-identical to the gateway's |
| `docs/harness-smart-hospital-operations.md` | The contract, the design decisions and the known limits |
| `docs/HARNESS_EXPLAINED.md` | This guide |
| `docs/harness-refrence.md` | The audit of the reference harness this was modelled on (kept untracked) |
| `backend/.env.example` | The harness settings, commented out |
| `CLAUDE.md`, `AGENT.md`, `rulebook.md`, `docs/ARCHITECTURE.md`, `docs/PROGRESS.md`, `SKILLS.md` | Project rules, agent behaviour, the timeline and how the work was delegated |

## 9. Files in the gateway repo

Repo: **hospital-gateway-private** (a fork of DeepSeek Harness, branch
`hospital-gateway`). Everything we added is under `hospital-gateway/`. DeepSeek's own
files are untouched, except that its GitHub workflows were removed.

### Source (`hospital-gateway/src/`)

| File | Role | Why it is there |
|---|---|---|
| `plugin.ts` | The plugin dsh loads. After dsh says it is ready it checks the setup (token, forbidden services, model exists), then starts the gateway. Exit code 78 for a configuration fault, 1 if it cannot listen. Exits itself if its launcher disappears | The entry point |
| `gateway.ts` | The HTTP server: `POST /v1/turns` and `GET /healthz`. Checks the token first, body size, and the number of turns at once | The door the backend calls |
| `agent-driver.ts` | Runs one turn: checks the model matches, builds a fresh dsh agent, applies the round limit, deadline and watchdog, collects the rounds and usage, builds the finish payload. Never throws | The heart of the gateway |
| `tool-proxy.ts` | Registers one stand-in tool per backend tool and forwards each call to the backend | Keeps all tool rules in the backend |
| `raw-args.ts` | Collects the exact argument text the model streamed | dsh repairs broken JSON; we need the original so the backend can reject it |
| `seed.ts` | Turns the backend's chat history into dsh's session format | So the agent starts with the conversation so far |
| `turn-state.ts` | What a turn has decided so far; the first stop reason wins and the backend's `end_turn` beats it | Stops later effects overwriting the real cause |
| `backend.ts` | The client for the backend's tool-call route | Typed errors, token header, no redirects |
| `protocol.ts` | The types and checks for every message, with size bounds | Rejects malformed data without echoing it |
| `config.ts` | The gateway's settings schema | Every field required, loopback host only |
| `log.ts`, `log-plugin.ts` | Whitelisted logging and the only log output | Logs ids and numbers, never message text |

### Launch and wiring

| File | Role |
|---|---|
| `bin/hospital-harness.mjs` | The launcher. Checks prerequisites, reads the secrets (environment first, then the backend `.env`), writes the dsh profile, links the gateway in with a Windows junction, starts dsh, waits until `/healthz` reports our model, forwards signals |
| `cordis.patch.yml` | The list of 12 dsh plugins that make up our composition, with the model route pinned |
| `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`, `tsconfig.json` | Tooling. The gateway is its own pnpm workspace and pins exact dsh versions |
| `vitest.config.ts`, `vitest.e2e.config.ts` | Unit and end-to-end test settings |
| `scripts/bootstrap.sh` | One-time setup: installs dsh's dependencies and ours |
| `scripts/check-contract-sync.mjs` | Fails if the example JSON files differ from the backend's |
| `contract/fixtures/*.json` (7 files) | The shared example messages |
| `README.md` | How to run it |

### Tests

| File | What it proves |
|---|---|
| `tests/unit/*.test.ts` (11 files) | Protocol checks, config, logging, seed, raw arguments, the backend client, the gateway server, the driver on real dsh with a scripted model, and the fakes themselves. 197 tests |
| `tests/fakes/fake-backend.ts` | A stand-in for the backend's tool-call route that behaves like the real one |
| `tests/fakes/mock-llm.ts` | A scripted model speaking the OpenAI streaming format |
| `tests/e2e/*.test.ts` (4 files) | The real launcher and real dsh against the fakes: startup, every stop reason, byte-exact arguments, no leaked text in logs, signals. 27 tests |

## 10. What we use from DeepSeek Harness

**The 12 plugins we mount** (in `cordis.patch.yml`):

| Plugin | Why |
|---|---|
| our `hospital-log` | The only log output |
| `timer` | A service the loop needs |
| `llm` | The model service; we check the model exists at startup |
| `llm-pi-ai` | Talks to OpenRouter; pinned base URL, key variable, timeouts and one model |
| `llm-retry` | One retry for network errors only |
| `session` and `session-projection` | Hold the conversation in memory for one turn |
| `system-prompt` | Its own text is switched off so the backend's prompt is used word for word |
| `tools` | The tool registry |
| `agent` | One agent per turn |
| `agent-loop` | The "call model, run tools, repeat" engine |
| our `hospital-gateway` | The plugin in `plugin.ts` |

**The dsh hooks we use** (in `agent-driver.ts` and `tool-proxy.ts`): creating and
driving an agent (`agents.create`, `followup`, `whenIdle`, `cancel`, `dispose`);
`agent/pre-step` for the round and deadline limits; `system-prompt/assemble` to keep
the backend's tool order; `agent/assistant-stream` for the raw arguments;
`session/event` for usage and the end of a turn; `tools/execute` and
`tools/pre-execute` for forwarding tools and blocking calls after the turn ended;
`concludeTurn()` to end the turn when the backend says so.

**What dsh has that we do not use:** shell, file, web, browser and computer tools;
MCP; sub-agents and workflows; planning and todo; skills; compaction; approvals and
the sandbox; session persistence; OpenTelemetry; the web GUI. The gateway refuses to
start if persistence, session telemetry or OpenTelemetry is mounted, so no
conversation text can be stored or exported by accident. DeepSeek's own safety notice
(`SAFETY.md` in the fork) applies to the full product; our gateway uses a small part.

## 11. Settings and how to run it

**Backend** (set in `backend/.env` or the environment; see `.env.example`):

| Setting | Default | Meaning |
|---|---|---|
| `AGENT_RUNTIME` | `in_process` | `harness` turns the harness on |
| `HARNESS_TOKEN` | none | Shared secret, at least 32 characters. Required when the runtime is `harness`; startup fails without it |
| `HARNESS_URL` | `http://127.0.0.1:8891` | Where the gateway listens (an http or https origin) |
| `HARNESS_MODEL` | `anthropic/claude-sonnet-5` | Must equal the gateway's model or every turn is refused |
| `HARNESS_MAX_TOKENS` | `4096` | Cap on one model round. Lower it if OpenRouter refuses with a 402 |
| `HARNESS_TURN_TIMEOUT_SECONDS` | `120` | Whole-turn budget, between 1 and 3600 |

**Gateway** (environment, set by you before launching):

| Variable | Default | Meaning |
|---|---|---|
| `HARNESS_TOKEN` | none | The same value as the backend's |
| `OPENROUTER_API_KEY` | none | The model key. Read from the environment, or from the backend `.env` if `HOSPITAL_BACKEND_DIR` is set |
| `HOSPITAL_BACKEND_DIR` | none | Root of this repo, used to find `backend/.env` |
| `HOSPITAL_HARNESS_PORT` | `8891` | Gateway port (host is always `127.0.0.1`) |
| `HOSPITAL_BACKEND_URL` | `http://127.0.0.1:8000` | The backend |
| `HOSPITAL_LLM_MODEL` | `anthropic/claude-sonnet-5` | The one model served |
| `HOSPITAL_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Provider endpoint |

**Run it** (PowerShell; use a long random token, for example from
`python -c "import secrets; print(secrets.token_urlsafe(48))"`):

```powershell
# 1. the gateway
cd C:\dev\harness-smart-agent\hospital-gateway
$env:HARNESS_TOKEN = "<the token>"
$env:HOSPITAL_BACKEND_DIR = "C:\Users\Amey\OneDrive\Desktop\Smart Hospital Operations Agent"
node bin/hospital-harness.mjs        # waits for: "hospital harness ready on http://127.0.0.1:8891"

# 2. the backend, in a second window
cd "C:\Users\Amey\OneDrive\Desktop\Smart Hospital Operations Agent\backend"
$env:AGENT_RUNTIME = "harness"
$env:HARNESS_TOKEN = "<the same token>"
$env:HARNESS_MAX_TOKENS = "1000"     # optional; for a small OpenRouter balance
.venv\Scripts\python.exe run.py

# 3. the UI, in a third window
cd "C:\Users\Amey\OneDrive\Desktop\Smart Hospital Operations Agent\frontend"
npm run dev                          # then open http://localhost:5173
```

**Check it** (use `curl.exe`, not the PowerShell `curl` alias):

```powershell
curl.exe http://127.0.0.1:8891/healthz      # gateway: {"status":"ok","model":...}
curl.exe http://127.0.0.1:8000/api/status   # the three lights
```

**Tests:** backend `cd backend; .venv\Scripts\python.exe -m pytest -m "not live_llm" -q`
(621 tests; on this machine set `PGHOSTADDR=127.0.0.1` or each database test waits
10 s). Gateway: `pnpm typecheck`, `pnpm test`, `pnpm test:e2e`, and
`pnpm contract:sync` (with `HOSPITAL_BACKEND_DIR` set). Frontend: `npm test`.

## 12. What can go wrong

| What happens | Who notices | What the user sees (`terminated_reason`) |
|---|---|---|
| The gateway is down or does not answer in time | `harness_bridge.py` | "The assistant is temporarily unavailable" (`harness_unavailable`) |
| The model uses all its rounds | `agent-driver.ts` | A fixed sentence (`max_rounds_exceeded`) |
| The deadline passes | the gateway's timer, then its watchdog | `llm_timeout` |
| Too many tool calls or too many invalid calls | `ToolTurn` in the backend | A fixed sentence (`max_tool_calls_exceeded`, `too_many_invalid_calls`) |
| The provider fails (for example OpenRouter 402 or 500) | the gateway | A fixed "something went wrong" (`harness_error`). The provider's text is never shown or logged |
| The model replies with blank text | the backend | Treated as `harness_error` |
| A wrong token on either door | the receiving side | An ordinary 404 |

The gateway's own reasons for an `error` finish are a fixed list: `model_mismatch`,
`seed_failed`, `create_failed`, `tool_call_failed`, `backend_closed`,
`truncated_tool_call`, `turn_aborted`, `turn_blocked`, `model_error`,
`turn_incomplete`, `watchdog`, `shutdown`, `internal_error`, `invalid_finish`.

**Timing budget** for a turn with deadline `D` (120 s): the gateway ends by `D - 2 s`;
its watchdog fires 20 s later (longer than one 15 s tool call, so it cannot abort a
call still committing); cleanup adds at most 10 s; the backend waits up to `D + 40 s`.
If you change any of these, change them together.

## 13. Known limits

- The Jev light shows configuration only; checking Jev live would cost a paid call on
  every refresh. The Agent light means a key is set, not that the key has credit.
- A final answer cut off by the token cap is reported as complete (the in-process
  agent behaves the same). Only a cut-off *tool call* is an error.
- If the gateway fails after a write already committed, the user sees "unavailable"
  even though the write happened; the stored history does record it.
- In harness mode the per-round `llm_response` rows are not written to the trace.
- The gateway's self-exit when its launcher dies is not exercised on Windows.
- The backend still listens on all network interfaces and has no sign-in on its API.
  This was true before the harness. Keep it on a trusted network or bind it to
  localhost, and never expose `/internal/` through a proxy.
- DeepSeek Harness is a pre-stable, unaudited preview. The gateway pins exact
  version `0.2.1-alpha.2`; upgrading means re-running the test suites.

# Harness runtime for the Smart Hospital Operations Agent

Branch `harness-smartgateway`. Status: **Part 1 (this repo) built, verified and committed. Part 2 (the
DeepSeek Harness fork, section 11) in progress.** Pattern reference:
`docs/harness-refrence.md` (an audit of the tattrex/agentic-pacs harness). This
document is the design for *this* project, what changed and why, and the
**contract** both halves implement. Sections marked **[contract]** change only by
editing this file first.

## 1. What and why

The agent's model loop can run in a separate process (the *harness*, a plugin
for DeepSeek Harness, "dsh") while every rule about what a tool call *does*
stays in this backend. `AGENT_RUNTIME=in_process` (the default) is the existing
LangGraph loop and is unchanged in behaviour. `AGENT_RUNTIME=harness` sends the
model loop to the harness.

Why: the loop (model calls, streaming, rounds, deadline) becomes swappable and
independently testable, while the safety rules (validation, grounding, caps,
audit, redaction) stay in one place that both runtimes share, so they cannot
drift. That is the reference's central idea, taken whole.

### How this project differs from the reference (and what that removed)

| tattrex reference | This project | Consequence |
|---|---|---|
| Browser Socket.IO terminates at the harness | UI talks `POST /api/chat` / voice WebSocket to the backend only | No relay, socket door, step fence, upstream sockets, cookie binding or nginx work |
| Tools run on a command thread with its own event loop | Tools are async on FastAPI's loop, fresh DB session per call | No cross-loop channel (`HarnessTurn`); the tool-call route calls the turn directly |
| Harness starts every turn | `handle_chat_message()` owns routing | Backend calls the harness only **after** parser, domain gate, eligibility and Jev; the harness is unreachable from anything else |
| Streamed `agent_text` | `ChatResponse` is request/response | v1 returns final text only; streaming is a later phase |
| `/finish` callback | Not needed | The finish payload is the response of `POST /v1/turns` |
| Backend hello route and poller | Not needed | Harness exposes `/healthz`; a model mismatch is detected per turn |

## 2. Shape

```
POST /api/chat | voice WS -> handle_chat_message()
  parser -> domain gate -> eligibility -> Jev        (unchanged, backend only)
  -> agent step, switched by AGENT_RUNTIME
       in_process: run_agent() / LangGraph          (default)
       harness:    run_agent_via_harness()
                     POST {HARNESS_URL}/v1/turns    {turn_id, system, history, user, tools, llm}
                       harness model loop (dsh)
                         --> POST {backend}/internal/v1/turns/{turn_id}/tool_calls
                               ToolTurn.call(): validation, grounding, caps,
                               timeout, CommandRunner, agent_events, spans
                       <-- finish payload (response body)
                     rebuild messages, write to checkpoint, return AgentState
```

`ToolTurn` (`backend/app/agent/turn.py`) is the model-free class extracted from
`graph.py`'s tool node. LangGraph's tool node and the internal route both call
it. It is this project's `AgentTurn`.

## 3. Part 1 components (this repo)

| File | Role |
|---|---|
| `app/agent/turn.py` | `ToolTurn`: counters, caps, unknown/invalid handling, per-call timeout, grounding rejection, `record_event`, spans and metrics, touched entity codes. No model, no LangGraph. |
| `app/agent/graph.py` | `tool_node` becomes a thin caller of `ToolTurn.call`. Gains `record_turn_messages` (generalised from `record_non_agent_turn`). Behaviour unchanged. |
| `app/agent/harness_registry.py` | In-memory `turn_id -> turn` registry for the lifetime of one `/v1/turns` call. |
| `app/agent/harness_bridge.py` | `run_agent_via_harness()`: payload build, HTTP call, finish mapping, message rebuild, checkpoint write-back, failure handling. Returns the same `AgentState` shape as `run_agent`. Registers a small wrapper around `ToolTurn` that records each served call as `(round, call_id, name, content)`, which the rebuild needs; `ToolTurn.call` itself takes no `call_id`. |
| `app/api/routes/internal_harness.py` | `POST /internal/v1/turns/{turn_id}/tool_calls`. |
| `app/agent/history.py` | Gains LangChain-to-OpenAI-message conversion for the payload. |
| `app/api/routes/chat.py` | Only the runtime switch at the agent step. No other routing change. |
| `app/config.py`, `app/main.py` | Four settings; router include. |

No schema change and no migration: the registry is in memory, and
`reschedule_appointment` still goes through `CommandRunner` and its audit row.

## 4. Settings

| Setting (env) | Default | Meaning |
|---|---|---|
| `AGENT_RUNTIME` | `in_process` | `in_process` or `harness` |
| `HARNESS_URL` | `http://127.0.0.1:8891` | Where the backend reaches the harness |
| `HARNESS_TOKEN` | unset | Shared secret, at least 32 characters. Local only; **not** a paid API key. Required when `AGENT_RUNTIME=harness`; startup **fails fast** if it is missing or short (`Settings` validator) |
| `HARNESS_MODEL` | `anthropic/claude-sonnet-5` | The model id the harness must serve. Sent per turn; the harness refuses a mismatch |
| `HARNESS_MAX_TOKENS` | `4096` | `llm.max_tokens` sent per turn: the cap on one model round. OpenRouter refuses a request whose cap the account balance cannot cover (HTTP 402), so lower it for a small balance |
| `HARNESS_TURN_TIMEOUT_SECONDS` | `120` | Whole-turn budget. Sent as `deadline_in_ms`; the backend's HTTP timeout is this plus a fixed grace |

`LLM_TIMEOUT_SECONDS` applies to the in-process loop only. In harness mode the
whole-turn deadline replaces it (documented difference).

## 5. [contract] Backend to harness: `POST {HARNESS_URL}/v1/turns`

Header `x-harness-token: <token>`. Body (JSON):

```json
{
  "turn_id": "<32 hex chars, uuid4().hex>",
  "system": "<system prompt text, including remembered preferences>",
  "history": [ {"role": "user|assistant|tool", "content": "...",
                "tool_calls": [{"id": "...", "type": "function",
                                "function": {"name": "...", "arguments": "<json string>"}}],
                "tool_call_id": "..."} ],
  "user": "<the new user message>",
  "tools": [ {"type": "function",
              "function": {"name": "...", "description": "...", "parameters": {}}} ],
  "llm": {"model": "<HARNESS_MODEL>", "max_tokens": "<HARNESS_MAX_TOKENS>",
          "deadline_in_ms": 120000, "max_rounds": 6}
}
```

- `history` never contains a system message and never contains the new `user`
  message. `tool_calls` only on assistant messages, `tool_call_id` only on tool
  messages.
- `tools` is `all_tool_dicts()` **in registry order** (stable prompt-cache prefix).
- The harness takes `max_rounds`, `deadline_in_ms` and `model` from here every turn.

Response `200` body (the finish payload):

```json
{
  "turn_id": "<same>",
  "stop_reason": null,
  "error": null,
  "model_text": "<final assistant text>",
  "rounds": 2,
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0},
  "assistant_rounds": [
    {"round": 1, "text": "", "tool_calls": [{"id": "...", "name": "...", "arguments": "<raw string>"}],
     "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}},
    {"round": 2, "text": "<final text>", "tool_calls": [],
     "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}}
  ]
}
```

A tool call may also carry an optional `result` (a string). It is present only for
a **gateway-local tool** (section 11.7): one the gateway ran itself, so the backend
never served it and could not otherwise know what the model saw. The backend uses
it to rebuild history; a call with neither a served result nor a `result` is
recorded as not executed, as before.

Each round's `usage` is that model request's own token count, so the turn's
`usage` is the sum of its rounds. The gateway always sends it; the backend treats
a missing per-round `usage` as zeros, so an older gateway still works. The backend
records it on one `llm_response` trace row per round (`input_tokens` =
`prompt_tokens`, `output_tokens` = `completion_tokens`), in both runtimes.

`stop_reason` is `null` (completed) or one of `"round_limit"`, `"time_limit"`,
`"error"`. When `"error"`, `error` is a non-empty string of at most 2000
characters; otherwise `null`. `arguments` are the model's **raw** argument
strings (not re-serialised), byte for byte.

Non-200 (as the gateway implements it): `404` token refused or unknown path
(identical body), `405` wrong method, `413` body too large, `400` bad body, `429`
busy or closing, `500` internal failure. Any non-200, transport error, timeout or malformed body is
"harness unavailable" to the bridge.

## 6. [contract] Harness to backend: `POST /internal/v1/turns/{turn_id}/tool_calls`

Header `x-harness-token`. Body: `{"round": 1, "call_id": "...", "name": "...",
"arguments": "<raw json string>"}` (`round` 1 to 10000, `arguments` up to 1 MB).

Response `200`: `{"content": "<tool message content>", "end_turn": null}` where
`end_turn` is `null`, `"max_tool_calls_exceeded"` or `"too_many_invalid_calls"`.
`content` is exactly what the model must see as the tool result.
After an `end_turn`, the harness stops the loop; any later call for that turn
receives the "Not executed" closer content and the same `end_turn`.

Statuses: `404` for an unknown or finished turn **and** for a missing/wrong
token (identical body `{"detail":"Not Found"}`, so the surface is not
advertised); `422` bad body; `409` is not used.

Auth rules: compare with `hmac.compare_digest` on UTF-8 bytes; check the token
**before** parsing the body or the turn id. The route takes `Request` and
validates the body itself after the token check (a malformed body therefore
cannot turn a stranger's 404 into a 422). The `/internal/` prefix must never be
published by a reverse proxy.

Deliberately not built (reference features that do not apply here): bad-token
throttle, cookie binding, hello route, socket door, step fence, channel thread.

## 7. Behaviour rules

1. **Backend owns safety.** Tool-call cap (`MAX_TOOL_CALLS`), invalid-call cap
   (`MAX_INVALID_TOOL_CALLS`), per-tool timeout, grounding, validation, events and
   audit live in `ToolTurn` and are identical in both runtimes. The harness owns
   rounds and the deadline.
2. **`end_turn` wins.** A backend `end_turn` is authoritative over any harness
   stop reason. Mapping to `terminated_reason`: `max_tool_calls_exceeded`,
   `too_many_invalid_calls` (as today); harness `round_limit` ->
   `max_rounds_exceeded`; `time_limit` -> `llm_timeout`; `error` ->
   `harness_error`; unreachable -> `harness_unavailable`.
3. **Unparseable arguments** count as an invalid call (`invalid_argument`), same
   as failed Pydantic validation today. `ToolTurn.call` takes `args: dict | None`;
   `None` means "could not parse".
4. **Fixed user-facing text on harness failure.** Provider or harness error text
   is neither shown nor logged (it can echo prompts, and redaction is
   fail-closed); only `stop_reason` and the error's length are logged. The reply
   is a fixed sentence per reason.
5. **History stays one conversation.** After the finish, the bridge rebuilds the
   turn as LangChain messages (assistant tool-call messages, one tool message per
   call, closers for calls that never ran, a final assistant message) and writes
   them to the checkpoint, so either runtime can continue the thread. A stored
   turn always ends with an assistant message. Calls are matched by
   `(round, call_id)`: providers reuse ids across rounds.
6. **System prompt per turn.** The payload carries a fresh system prompt (current
   preferences). Stored system messages are stripped from `history`.
7. **No silent fallback** to the in-process runtime. If the harness is down the
   user gets the fixed unavailable message and an `agent_events` row
   (`harness_unavailable`, FAILURE). Parser, Jev and the gates are unaffected.
8. **Gates unchanged.** The harness path sits where `run_agent` sits: after the
   domain gate, eligibility gate and Jev. No gate is weakened.
9. **Trace panel unchanged.** The bridge writes `agent_invoked` at turn start and
   the same terminal events as today (`max_rounds_exceeded`, `llm_timeout`);
   tool events come from `ToolTurn` as before. Per-round `llm_response` rows are
   not written in harness mode (known, documented difference).
10. **Observability.** One `harness.turn` span under `agent.run` (attributes:
    rounds, stop_reason, model; never text). `tool.execute` spans are created by
    `ToolTurn` as before. Metric labels stay low-cardinality; `record_agent_turn`
    is called as today. Logs about the harness carry ids and enums only.
11. **Registry lifetime.** The turn is registered before the POST and removed in
    `finally`, after which the bridge waits for any call still running so the
    served calls are final. A tool call for an absent turn is a 404.
12. **Calls are serialised and idempotent.** Tool calls of one turn run one at a
    time (as in the graph), so parallel requests cannot overshoot a cap. A
    repeated `(round, call_id)` returns the stored content and does not run the
    tool again, which matters for `reschedule_appointment`.
13. **A blank reply is an error.** A clean finish whose `model_text` is blank is
    mapped to `harness_error` with the fixed sentence, never shown as an empty
    bubble.
14. **Tool spans** (`tool.execute`) are parented on the `harness.turn` span.

## 8. Verification (Part 1)

1. Existing suite passes unmodified after the `ToolTurn` extraction.
2. Unit tests for `ToolTurn` alone (no model, no graph).
3. Contract fixtures in `backend/tests/fixtures/harness_contract/` validated
   against strict Pydantic models (tests only; routes return plain dicts).
4. A backend "world" test: the real FastAPI app and real test DB, with a fake
   harness (Python) that plays the harness protocol over the ASGI client. It
   proves the full chain with no model: tool calls, caps, end_turn, history
   write-back, harness-down handling, token refusal.
5. App check: start the backend in `harness` mode against a stub harness and run
   a real request.
6. Code review of the whole branch diff before anything is committed.

Part 2 (fork, `hospital-gateway/`) builds the Node side against the same
fixtures; a sync script compares both fixture copies (parsed equality, key order,
file sets, no values printed). Not started.

## 9. Known limits (Part 1)

- In harness mode the per-round `llm_response` rows are written when the turn
  finishes (that is when the token counts arrive), so in the trace panel they sort
  after that turn's `tool_executed` rows instead of between them. The round numbers
  on the rows are correct.
- A harness-side failure after a write tool committed shows the user the fixed
  "unavailable" sentence even though the write happened; the stored history
  records the served call and its result, so the next turn knows.
- A served call the harness never reported is rebuilt with empty arguments
  (only the name is recorded).
- Log lines from the internal route carry the internal request's id, not the
  chat request's `session_id`.

## 10. Progress and decisions log

See `docs/PROGRESS.md` ("Harness runtime") for the timeline. Decisions made here
rather than silently: token fail-fast instead of the reference's degrade (our
config module already fails fast); no throttle; no hello route; no streaming in
v1; per-round LLM timeout not enforced by the harness (whole-turn deadline only).

## 11. Part 2: the gateway (`hospital-gateway/` in the fork)

Fork: `https://github.com/Ameyk244/harness-smart-agent` (public fork; MIT),
cloned at `C:\dev\harness-smart-agent`, branch `hospital-gateway` on top of the
upstream tag `dsh-v0.2.1-alpha.2` (`d743267`), with only upstream's workflows and
Dependabot config removed. **dsh's own source is never edited**; everything we add
lives in `hospital-gateway/`. The folder is its own pnpm workspace root with its
own lockfile (as in the reference), so pnpm never adopts dsh's workspace above it.

### 11.1 What it is

A dsh plugin ("bundle") that exposes `POST /v1/turns` and `GET /healthz` on
loopback. For each turn it creates a fresh in-memory dsh agent, seeds it with the
backend's `system`/`history`, registers one **proxy tool** per backend tool, and
runs the model loop. Every tool call is forwarded to the backend's
`/internal/v1/turns/{turn_id}/tool_calls` (section 6); the harness executes
nothing itself. The response is the finish payload (section 5).

Smaller than the reference's gateway on purpose: no browser sockets, so no relay,
upstream sockets, step fence, hello poller or pre-attach buffer; no streaming
(`agent_text`) so no stream mapper, only a **raw-argument collector**.

### 11.2 Modules (`hospital-gateway/src/`)

| File | Role |
|---|---|
| `config.ts` | Settings read from the launcher-provided environment; all required |
| `protocol.ts` | Types and boolean guards for the section 5 and 6 bodies. Inbound tolerates extra keys, fails on missing keys/wrong types/unknown enums; outbound is checked strictly. Guards never throw and never echo values |
| `backend.ts` | Typed client: `toolCall(turnId, body) -> {content, end_turn}`. `x-harness-token`, `redirect:'error'`, 90 s timeout, errors carry status/kind only. A 404 means the backend closed the turn |
| `seed.ts` | OpenAI-format `history` to dsh `SessionEvent[]` (reference 5.8): contiguous `seq` from 0, system node at seq 0 is mandatory, skip empty tool-call ids and their orphaned results, skip empty assistant messages |
| `tool-proxy.ts` | One dsh tool per backend tool, in backend order, raw JSON-Schema parameters, no argument validation in the harness. `execute` forwards the model's **raw** argument string; a backend `end_turn` sets state and calls `concludeTurn()` |
| `raw-args.ts` | Collects each tool call's provider argument string from `tool-call-delta.argumentsDelta` (dsh's `tool/call` carries pi-ai's repaired JSON) |
| `agent-driver.ts` | `run(request) -> FinishPayload`; never rejects. Preflight, seed, create, drive, map |
| `gateway.ts` | HTTP server: token check first, body cap, concurrency cap, `/healthz`, shutdown |
| `log.ts`, `log-plugin.ts` | Whitelist logger and the only console exporter (reference 5.12) |
| `plugin.ts` | dsh plugin entry: startup checks, fatal exit codes, orphan watchdog |
| `bin/hospital-harness.mjs` | Launcher (reference 3.4) with a Windows **junction** instead of a directory symlink |
| `cordis.patch.yml` | 12-row standalone composition: `hospital-log`, `timer`, `llm`, `llm-pi-ai`, `session`, `session-projection`, `system-prompt`, `tools`, `agent`, `llm-retry`, `agent-loop`, `hospital-gateway`; no persistence, telemetry, web or coding rows, checked at startup |
| `scripts/check-contract-sync.mjs` | Compares `contract/fixtures/` with this repo's `backend/tests/fixtures/harness_contract/` (parsed equality, key order, file sets; prints paths, never values; exit 0/1/78) |

### 11.3 Configuration (environment, set by the launcher)

| Variable | Default | Meaning |
|---|---|---|
| `HARNESS_TOKEN` | none, required, at least 32 chars | Same name and value as the backend's setting; read from the environment first, then from `<HOSPITAL_BACKEND_DIR>/backend/.env` (never printed). The backend `.env` has no `HARNESS_TOKEN` yet, so export it or add it |
| `OPENROUTER_API_KEY` | none, required | Read the same way |
| `HOSPITAL_BACKEND_DIR` | none | Root of the backend repo (the folder holding `backend/`); used for the `.env` lookup and by `contract:sync`. No default: the two repos live in different places on this machine |
| `HOSPITAL_HARNESS_HOST` / `PORT` | `127.0.0.1` / `8891` | Listen address (loopback only) |
| `HOSPITAL_BACKEND_URL` | `http://127.0.0.1:8000` | Backend base URL |
| `HOSPITAL_LLM_MODEL` | `anthropic/claude-sonnet-5` | The one model served; must equal the backend's `HARNESS_MODEL` or every turn finishes with `error` and no model call |
| `HOSPITAL_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Provider base URL |

The backend sends sampling and limits per turn (`llm.max_tokens`,
`deadline_in_ms`, `max_rounds`); the harness takes them from the request.

Found while building against the real dsh source (it wins over the reference):
`llm-pi-ai` takes `providers: {openrouter: {...}}` and a `models` list that
replaces the catalog; dsh checks only `@deepseek-ai/dsh-*` peers against the
runtime version (the `cordis`/`schemastery` pins are enforced by
`manifest.test.ts`); `session-projection` lives at
`packages/session/session-projection`; the gateway row also carries `provider`,
because `agents.create` needs it; the launcher refuses any `.env` in the dsh root
or `DSH_HOME`, has no `--patch`/overlay flags, and adds the Windows variables dsh
needs to its child-environment whitelist; the log exporter has no `dshText`
switch (dsh-internal text can contain prompts, so only a digest and length are
printed).

### 11.4 Decisions this design makes where the contract was silent

1. **`stop_reason` mapping.** Clean completion or backend `end_turn` -> `null`;
   round cap -> `round_limit`; deadline -> `time_limit`; everything else
   (model mismatch, seed failure, create failure, tool transport failure,
   watchdog, dsh error/aborted/blocked, a round cut off at `max_tokens` that had
   tool calls) -> `error` with a short fixed-vocabulary text, at most 2000 chars,
   never provider text. A backend `end_turn` always beats a harness reason.
2. **Truncated tool calls are not forwarded.** dsh drops tool-call blocks of a
   round cut off at `max_tokens`; unlike the reference we do not recover and run
   them. The turn ends with `error`. (Fewer moving parts; the backend never
   executes a half-written call.)
3. **Unknown tool names** are forwarded to the backend, which rejects and counts
   them, so the invalid-call cap behaves exactly as in-process.
4. **One agent per turn, fresh, in memory.** Nothing is persisted; history comes
   from the request every turn.
5. **Caps.** dsh has no round or time cap: `agent/pre-step` rejects past
   `max_rounds` or the deadline; a timer cancels the agent; a watchdog bounds a
   stuck loop. `maxParallelToolCalls: 1`.
6. **Prompt-cache stability.** Tool order is the request's order (dsh sorts by
   name; restore it in `system-prompt/assemble`); temperature and max tokens are
   set through `agent/request`.
7. **PHI-safe logging.** Whitelisted ids and enums only; never message text, tool
   arguments, prompts, history, tokens or error messages.
8. **Fatal exit codes.** 78 for a configuration fault (missing/short token, no
   key, forbidden service mounted, model unresolved); 1 for a listen failure.

### 11.5 Verification (Part 2)

1. Unit tests with fakes (fake backend implementing section 6 from the shared
   fixtures; scripted mock LLM speaking the OpenAI streaming format).
2. E2E through the real launcher and real dsh against the fake backend and mock
   LLM, in a fresh temp `DSH_HOME`, asserting exit codes, the ready line and
   that no log line contains PHI-like content.
3. `contract:sync` against this repo's fixtures.
4. Integration against the **real backend** (`AGENT_RUNTIME=harness`) with the
   mock LLM: the full chain, no live model.
5. One live smoke with the real model (the only paid call), then the app check
   in all modes: parser, Jev, `in_process`, `harness`.

### 11.6 Timing budget and known limits (Part 2)

Budget for one turn with deadline `D` (the backend sends 120 s): the harness ends
by `D - 2 s` (margin); a watchdog fires 20 s later, longer than one backend tool
call may run (15 s), so it cannot abort a call that is still committing; bounded
cleanups add at most 5 s + 5 s. The backend's HTTP timeout is `D + 40 s`
(`HTTP_GRACE_SECONDS`), which holds that worst case with room to spare. If any of
these constants change, change them together.

Known limits, deliberately left as they are:
- A final answer cut off at `max_tokens` (no tool calls) is returned as a normal
  completion, the same as the in-process runtime; only a cut-off tool call is an
  error (11.4 item 2).
- `history` tool-call arguments reach the model re-serialised compactly (dsh and
  pi-ai parse and stringify them). The current round's arguments are forwarded
  raw and byte-exact, which is what the contract needs.
- `deadline_in_ms` above one hour is refused (Node timers overflow at 2^31 ms).
- The launcher's readiness check verifies the health body's model, so a different
  harness on the port cannot pass; an earlier gateway with the *same* model still
  on the port could briefly, and the launcher then exits 1 when its own child
  fails to bind.
- The gateway's orphan watchdog is not exercised on Windows, where killing the
  launcher also kills dsh; it needs a POSIX run to verify.
- The outbound tool-call guard (`isToolCallRequest`) is exercised by tests and the
  fixtures, not called at runtime; an oversized argument string would be refused
  by the backend (422) and end the turn.
- Port 8890 is taken by an unrelated harness on the development machine, so this
  project's default is 8891.

### 11.7 Gateway-local tools (opt-in)

A tool that is **harmless by construction** may run inside the gateway instead of
the backend: it takes no hospital data, changes nothing, and needs no grounding.
It is the way to learn dsh's own tool authoring (`defineTool` from `dsh-tools`).
The first one is `current_time`, which returns the current date, time and weekday
so the model can resolve "tomorrow at 9" for `reschedule_appointment`.

- **Off by default.** `HOSPITAL_LOCAL_TOOLS` (comma-separated names, default empty)
  turns them on; with it unset the gateway behaves exactly as before.
- **Where they sit.** After the backend's tools in the prompt. A local tool whose
  name equals a tool in the request is skipped (the backend's wins).
- **What the backend sees.** Nothing at call time: no `tool_executed` row, no count
  against `MAX_TOOL_CALLS`. They count as rounds, which the round limit bounds. The
  call appears in `assistant_rounds[].tool_calls[]` with its `result`, so the stored
  history is complete (fixture `turns.response.local_tool.json`).
- **Never** put anything here that reads or writes hospital data, runs a command or
  reaches the network: that would bypass grounding, caps and audit.
- It exists only in the harness runtime; the in-process agent does not have it.

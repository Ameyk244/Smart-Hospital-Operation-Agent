# Harness runtime for the Smart Hospital Operations Agent

Branch `harness-smartgateway`. Status: **Part 1 (this repo) built and verified (offline suite, app
check in both runtimes, code review); Part 2 (the DeepSeek Harness fork) not
started.** Pattern reference:
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
| `HARNESS_URL` | `http://127.0.0.1:8890` | Where the backend reaches the harness |
| `HARNESS_TOKEN` | unset | Shared secret, at least 32 characters. Local only; **not** a paid API key. Required when `AGENT_RUNTIME=harness`; startup **fails fast** if it is missing or short (`Settings` validator) |
| `HARNESS_MODEL` | `anthropic/claude-sonnet-5` | The model id the harness must serve. Sent per turn; the harness refuses a mismatch |
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
  "llm": {"model": "<HARNESS_MODEL>", "max_tokens": 4096,
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
    {"round": 1, "text": "", "tool_calls": [{"id": "...", "name": "...", "arguments": "<raw string>"}]},
    {"round": 2, "text": "<final text>", "tool_calls": []}
  ]
}
```

`stop_reason` is `null` (completed) or one of `"round_limit"`, `"time_limit"`,
`"error"`. When `"error"`, `error` is a non-empty string of at most 2000
characters; otherwise `null`. `arguments` are the model's **raw** argument
strings (not re-serialised), byte for byte.

Non-200: `401/404` token refused, `409` busy-state violation, `422` bad body,
`5xx` harness failure. Any non-200, transport error, timeout or malformed body is
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

- Per-round `llm_response` rows are not written in harness mode.
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

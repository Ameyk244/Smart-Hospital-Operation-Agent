# Jev Fast Path

Jev is a read-only routing layer between the deterministic parser and the
LangGraph agent. It handles natural-language variations of a small set of
known commands without spending a full agent turn. It is not an agent tool,
does not replace the parser, and does not own hospital business logic.

## Request Flow

```text
POST /api/chat
  -> deterministic parser
  -> domain gate
  -> eligibility gate
  -> Jev typed decision
       -> confident supported read: CommandRunner
       -> decline, low confidence, timeout, or error: LangGraph agent
```

The placement is deliberate. Off-topic or invalid requests are rejected
before any paid model call. Jev can optimize only a request that would
otherwise reach the fallback agent.

## Supported Reads

Jev can route these canonical operations:

| Operation | Optional arguments |
|---|---|
| `list_departments` | None |
| `list_scanners` | Modality and scanner status |
| `show_next_appointment` | None |
| `list_delayed_appointments` | Modality |

`search_patients` may be recognized but is not executed because its free-text
name cannot be safely produced by the fixed-choice decision schema. All
writes, multi-step requests, preferences, and contextual reasoning continue
to the LangGraph agent. Jev cannot reschedule an appointment.

## Decision Contract

`backend/app/agent/jev_fast_path.py` asks Jev for typed choices rather than a
free-form answer. The main choice selects a known command or `none`; additional
choices represent scanner modality and status where needed. A command runs
only when the command confidence reaches `JEV_CONFIDENCE_THRESHOLD`, which is
`0.9` by default. Uncertain optional filters are omitted instead of guessed.

Every match uses the existing `CommandRunner`, so repositories, transactions,
validation, and response formatting are shared with exact parser commands.
Jev has no SQL or repository access of its own.

## Failure And Safety Behavior

- Domain and eligibility checks always run before Jev.
- Unsupported commands and arguments decline to the agent.
- Low confidence declines to the agent.
- A timeout after `JEV_TIMEOUT_SECONDS` declines to the agent.
- Missing SDK configuration, malformed responses, and provider errors decline
  to the agent and are traced.
- No Jev decision creates grounded entities or bypasses grounding.
- No database schema change is required.

## Configuration

```dotenv
ENABLE_JEV_FAST_PATH=true
TYPESAFE_API_KEY=
JEV_MODEL=jev-latest
JEV_CONFIDENCE_THRESHOLD=0.9
JEV_TIMEOUT_SECONDS=5.0
```

Set `ENABLE_JEV_FAST_PATH=false` to restore the direct parser-miss-to-agent
flow. `TYPESAFE_API_KEY` is a secret and must stay in the local `.env` or the
deployment provider's secret environment settings, never in Git.

For Render, `render.yaml` declares the non-secret values and a secret slot for
`TYPESAFE_API_KEY`. Existing Blueprints may require that secret to be entered
manually on the backend web service before redeploying.

## Examples

Likely Jev candidates:

```text
show me all the departments
can you pull up the department list for me?
which appointments are running late?
how many delayed MRI appointments are there?
which MRI scanners are available?
what is the next scheduled appointment?
```

Requests that must continue to the agent:

```text
find delayed MRI appointments and move the first one
reschedule APT-2002 to an available MRI scanner
remember that I prefer morning appointments
what did you just change?
```

Off-topic requests remain rejected before Jev:

```text
what is 47 times 12? MRI
write a poem about patients
```

## Observability And Cost

Each consultation records a `jev_invoked` event with its outcome, selected
command, confidence, token usage, model, and latency. The session trace UI
renders those events and labels successful chat responses with
`handled_by="jev"`.

`GET /api/cost-comparison` and the frontend cost page aggregate measured Jev
usage and compare it with an estimated agent-only baseline. Avoided agent cost
is necessarily an estimate because those agent calls were not made.

## Verification

Normal tests use a fake TypeSafe SDK and consume no provider credits. The main
coverage is in:

- `backend/tests/unit/test_jev_fast_path.py`
- `backend/tests/e2e/test_jev_fast_path_api.py`
- `frontend/src/components/CostComparisonPage.test.tsx`
- frontend chat and trace component tests

Live-provider tests remain explicitly gated and should be run only when a real
integration check is intended.

## Source Map

| Concern | File |
|---|---|
| Typed decision and SDK boundary | `backend/app/agent/jev_fast_path.py` |
| Request routing | `backend/app/api/routes/chat.py` |
| Settings | `backend/app/config.py` |
| Cost aggregation API | `backend/app/api/routes/cost.py` |
| Response schema | `backend/app/api/schemas/chat.py` |
| UI badge and trace | `frontend/src/components/ChatPanel.tsx`, `TracePanel.tsx` |
| Cost page | `frontend/src/components/CostComparisonPage.tsx` |

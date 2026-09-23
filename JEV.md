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
free-form answer. All questions are included in one `system_one()` call with
the original user text supplied as `state`.

### Why This Project Uses Choice

TypeSafe's System One API supports three primary question primitives:

| Primitive | Meaning | Answer shape | Used by this project? |
|---|---|---|---|
| `Choice` | Select one option from a defined set | Winning label, confidence, and a probability per label | Yes, for all four Jev questions |
| `Noul` | Decide whether one condition is true | Probability of yes from `0` to `1` | No |
| `Score` | Rate the state against ordered rubric levels | Probability-weighted numeric score, confidence, legend, and level probabilities | No |

`Choice` matches this routing problem because each decision needs one
mutually exclusive categorical value: one command, one scanner modality, one
scanner status, or one appointment modality. Its fixed criteria also prevent
Jev from inventing arbitrary command names or arguments.

`Noul` would fit an independent binary question such as "does this request
mention urgency?" It is not used here because separate yes/no questions for
every command or modality could overlap and would require additional conflict
resolution before constructing one command.

`Score` would fit an ordered rubric such as low, medium, and high urgency. It
is not used because `MRI`, `CT`, and `XRAY`, or `list_scanners` and
`list_departments`, have no meaningful numeric order.

The `confidence` field returned with a `Choice` answer must not be confused
with the `Score` primitive. This project compares `ChoiceAnswer.confidence`
with `JEV_CONFIDENCE_THRESHOLD`; it never creates a `Score(...)` question or
reads a `ScoreAnswer.score` value. Likewise, the explicit labels `none` and
`unspecified` are ordinary `Choice` options defined by this application, not
`Noul` answers from a separate binary question.

### Questions And Choices

| Question key | Allowed labels | Used for |
|---|---|---|
| `command` | `list_departments`, `list_scanners`, `show_next_appointment`, `list_delayed_appointments`, `search_patients`, `none` | Primary routing decision |
| `scanner_type` | `MRI`, `CT`, `XRAY`, `unspecified` | `list_scanners.type` |
| `scanner_status` | `AVAILABLE`, `IN_USE`, `MAINTENANCE`, `unspecified` | `list_scanners.status` |
| `appointment_type` | `MRI`, `CT`, `XRAY`, `unspecified` | `list_delayed_appointments.appointment_type` |

Each `Choice` includes written criteria describing what every label means.
The explicit `none` command prevents conversational, ambiguous, multi-step,
or write requests from being forced onto the nearest read command. Similarly,
`unspecified` prevents an absent or ambiguous filter from being guessed.

`search_patients` appears in the command choices but is intentionally absent
from the executable command set. Recognizing it produces an honest routing
decision, but execution falls through to the agent because the fixed-choice
schema cannot return the required free-text patient name.

### Scoring Rules

Jev returns a selected label and confidence score for each question, plus an
optional probability map. The routing algorithm applies these rules:

1. Read the `command` answer.
2. If its confidence is below `JEV_CONFIDENCE_THRESHOLD` (`0.9` by default),
   return `low_confidence` and continue to the agent.
3. If it selected `none`, return `no_command_match` and continue to the agent.
4. If it selected a recognized but non-executable operation such as
   `search_patients`, continue to the agent.
5. For an executable operation, inspect only the filter questions mapped to
   that command.
6. Include a filter only when its own confidence reaches the same threshold
   and its label is not `unspecified`.
7. Construct a typed `Command` and pass it to `CommandRunner`.

The primary command's probability map is recorded in the `jev_invoked` trace
for debugging and evaluation. Filter confidence scores are used while building
the command but are not currently persisted. The scores are not combined into
a second custom score; the implementation uses each selected answer's
confidence directly.

### Worked Example

For this message:

```text
which available MRI scanners are there?
```

a successful response may look like:

```text
command:          list_scanners  confidence: 0.98
scanner_type:     MRI            confidence: 0.97
scanner_status:   AVAILABLE      confidence: 0.96
appointment_type: unspecified    confidence: 0.99
```

Only the filters mapped to `list_scanners` are read, so the appointment answer
is ignored. The backend constructs:

```python
Command(
    "list_scanners",
    {"type": "MRI", "status": "AVAILABLE"},
)
```

If `scanner_status` scored `0.72`, the command could still run, but without
the uncertain status filter:

```python
Command("list_scanners", {"type": "MRI"})
```

Every match uses the existing `CommandRunner`, so repositories, transactions,
validation, and response formatting are shared with exact parser commands.
Jev has no SQL or repository access of its own.

The primitive behavior above follows TypeSafe's
[Python SDK documentation](https://docs.typesafe.ai/sdk/python),
[Python usage guide](https://docs.typesafe.ai/sdk/python/usage), and
[official Python SDK source](https://github.com/typesafe-ai/typesafe-sdk-python).

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
| Response schema | `backend/app/api/routes/chat.py` (`ChatResponse`) |
| UI badge and trace | `frontend/src/components/ChatPanel.tsx`, `TracePanel.tsx` |
| Cost page | `frontend/src/components/CostComparisonPage.tsx` |

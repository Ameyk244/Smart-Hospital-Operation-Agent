# Smart Hospital Operations Agent

A full-stack hospital operations system built around a deterministic-first architecture.

Exact operational commands are handled without an LLM. Natural-language read requests can take a confidence-gated Jev fast path into the same command layer, while richer requests fall back to a bounded LangGraph agent that can search hospital data, reason over tool results, and safely reschedule appointments using grounded entity references.

The project uses only synthetic patient and hospital data.

## Key Features

- Deterministic parser for common read operations
- Jev typed-decision fast path for near-miss read commands
- LangGraph agent as a natural-language fallback
- PostgreSQL-backed hospital and agent state
- Grounded appointment and scanner references
- Safe scanner reassignment with business-rule validation
- Persistent conversations and session preferences
- Agent execution traces visible in the frontend
- Synthetic, reproducible seed data
- Offline tests that do not consume LLM API credits
- Anthropic and OpenRouter provider support

## Architecture

```text
POST /api/chat
      |
      v
Deterministic parser
      |
   matched?
   /      \
 YES       NO
  |         |
  v         v
Command     Domain gate
Runner          |
  |         in domain?
  |         /       \
  |       NO         YES
  |       |           |
  |    Rejected   Eligibility check
  |                   |
  |               eligible?
  |               /      \
  |             NO        YES
  |             |          |
  |         Rejected   Jev fast path
  |                    /          \
  |             confident       decline/error
  |                 |               |
  |                 v               v
  |          CommandRunner     LangGraph agent
  |                              agent <-> tools
  |                                   |
  +-----------------------------------+
          |
          v
   Persist response
```

The exact deterministic path does not create a model client or consume model tokens. Jev is consulted only after the parser misses and both gates pass; a confident match runs a read command, while every decline or integration failure falls through to the agent.

The agent is activated only when:

1. The deterministic parser does not match.
2. The request belongs to the hospital operations domain.
3. The request passes eligibility checks.
4. Jev declines, is not confident, is disabled, or cannot handle the request.

## Agent Scope

The agent can work with:

- Departments
- Patients
- Appointments
- Appointment status
- MRI, CT, and X-ray scanners
- Scanner availability
- Scanner reassignment
- Explicit session preferences
- Contextual follow-up questions

## Commands and Agent Test Messages

Requests move through three execution layers in order, with rejection handled
before either model when the request is outside the hospital domain or fails
eligibility checks:

- **Deterministic parser:** exact read commands, no model call.
- **Jev fast path:** natural variations of supported reads, using one typed
  routing decision.
- **LangGraph agent:** richer, contextual, multi-tool, preference, and
  rescheduling requests.
- **Rejected:** off-topic or ineligible input stopped before Jev or the agent.

---

## Deterministic Commands

The regex parser handles these exact, case-insensitive command shapes. They
are read-only, execute through `CommandRunner`, return
`handled_by="deterministic"`, and consume no model tokens.

### Departments

```text
list departments
```

### Scanners

```text
list scanners
list scanners mri
list scanners ct
list scanners xray
list scanners available
list scanners in use
list scanners maintenance
list scanners mri available
list scanners ct in use
list scanners xray maintenance
```

### Patients

Replace `<name>` with a patient name.

```text
show patient <name>
show patient David
show patient Susan Anderson
```

### Appointments

```text
show next appointment
show the next appointment
list delayed appointments
list delayed appointments mri
list delayed appointments ct
list delayed appointments xray
```

---

## Jev Fast Path

Jev handles natural-language variations of known read commands that miss the
strict parser. After the domain and eligibility gates pass, the backend uses
TypeSafe AI's `typesafe-sdk` and its `system_one` API to request a typed,
confidence-scored command choice instead of a free-form response.

Jev can route these operations:

| Operation | What Jev can identify |
|---|---|
| `list_departments` | Department-list requests |
| `list_scanners` | Optional MRI, CT, or X-ray modality and scanner status |
| `show_next_appointment` | Requests for the next appointment |
| `list_delayed_appointments` | Optional MRI, CT, or X-ray modality |

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

### How Jev Chooses

The backend sends the user message as `state` in one `system_one()` request.

The request contains four typed `Choice` questions:

| Question key | Choices | Purpose |
|---|---|---|
| `command` | `list_departments`, `list_scanners`, `show_next_appointment`, `list_delayed_appointments`, `search_patients`, `none` | Identify one requested operation |
| `scanner_type` | `MRI`, `CT`, `XRAY`, `unspecified` | Optional `list_scanners` modality |
| `scanner_status` | `AVAILABLE`, `IN_USE`, `MAINTENANCE`, `unspecified` | Optional `list_scanners` status |
| `appointment_type` | `MRI`, `CT`, `XRAY`, `unspecified` | Optional delayed-appointment modality |

Jev returns a selected label, a confidence score, and optionally the
probability distribution for each question. The selected `command` must score
at least `JEV_CONFIDENCE_THRESHOLD` (`0.9` by default), must not be `none`, and
must be one of the four executable read operations. `search_patients` is
recognized so Jev can classify it honestly, but it falls through to the agent
because a fixed choice cannot safely provide the required patient name.

Optional filters are evaluated independently using the same threshold. A
filter is included only when its own answer is confident and not
`unspecified`; otherwise it is omitted rather than guessed. For example:

```text
User message: "which available MRI scanners are there?"

command:        list_scanners  confidence: 0.98
scanner_type:   MRI            confidence: 0.97
scanner_status: AVAILABLE      confidence: 0.96

Command("list_scanners", {"type": "MRI", "status": "AVAILABLE"})
```

The resulting typed `Command` is executed by the same `CommandRunner` used by
the parser. Jev never queries the database directly and does not generate the
final hospital data. The primary command's probability map is stored in the
`jev_invoked` trace for inspection; routing uses the selected labels and
confidence scores.
Low confidence, `none`, timeouts, provider errors, and unsupported choices all
fall through to the LangGraph agent.

This design follows TypeSafe's documented primitive semantics. See the
[official Python SDK guide](https://docs.typesafe.ai/sdk/python) and
[usage guide](https://docs.typesafe.ai/sdk/python/usage).

Jev cannot reschedule appointments or perform any other hospital write. It
also does not handle free-text patient searches, multi-step work, preferences,
or contextual follow-ups. Those remain agent responsibilities.

### Jev Fast-Path Messages

These are useful examples of natural language that misses the exact parser but can resolve to a supported read command:

```text
show me all the departments
can you pull up the department list for me?
which appointments are running late?
how many delayed MRI appointments are there?
which MRI scanners are available?
what is the next scheduled appointment?
```

Successful responses return `handled_by="jev"` and display the `jev` badge.
Each consultation records a `jev_invoked` trace event. See `JEV.md` for the
full decision, fallback, configuration, and observability contract.

---

## Agent Messages

The LangGraph agent is the final fallback for eligible hospital requests that
the parser and Jev do not resolve. It uses the configured Anthropic or
OpenRouter chat model, can reason over conversation context, and may call one
or more of the seven registered tools. Its hospital operations still pass
through `CommandRunner`, and rescheduling is its only hospital-data write.

### Appointment Search

```text
Find all delayed appointments
Find the delayed MRI appointments
Show me the delayed CT appointments
Which X-ray appointments are delayed?
Find appointments for patient PT-1002
Show appointments assigned to scanner SCN-1
Which appointments belong to the Radiology department?
What is the next scheduled appointment?
```

### Scanner Search and Availability

```text
Find all available MRI scanners
Which CT scanners are currently available?
Show me the scanners that are under maintenance
Which scanner is assigned to the first delayed MRI appointment?
Check whether scanner SCN-1 is available
Find another available scanner with the same modality
```

A scanner code must first be returned by a trusted search before the agent can request its detailed availability.

### Patient and Department Queries

```text
Find the patient named David Davis
Show me appointments for Susan Anderson
Which department handles the delayed MRI appointments?
List the scanners used by the Radiology department
Find appointments belonging to patient PT-1003
```

### Appointment Rescheduling

Rescheduling is the agent's only hospital-data write operation.

```text
Find the delayed MRI appointments and move the first one to another available MRI scanner
Move the first delayed CT appointment to an available CT scanner
Reschedule the appointment you found to another compatible scanner
Move that appointment to the available scanner
Keep the same time and change only the scanner
Move it to the other available MRI scanner
```

The agent validates that the appointment and scanner exist, the modalities match, and the target scanner is available.

---

## Multi-Tool Requests

These messages require the agent to combine multiple tools.

### Search and Compare

```text
Find delayed MRI appointments and show which available MRI scanners could handle them
Find the next delayed CT appointment and check for another available CT scanner
Show delayed appointments and identify compatible available scanners
Find Susan Anderson's delayed appointment and check its scanner status
```

### Search and Reschedule

```text
Find the first delayed MRI appointment, locate another available MRI scanner, and move the appointment
Find a delayed CT appointment and reschedule it to a compatible available scanner
Search for delayed appointments, choose the first one, and move it to another available scanner of the same type
```

A typical multi-tool flow is:

```text
search appointments
        |
        v
ground appointment and scanner codes
        |
        v
find or inspect a compatible scanner
        |
        v
validate grounded references
        |
        v
reschedule appointment
```

---

## Contextual Follow-Ups

The agent can understand short follow-ups when recent user messages establish hospital context.

Example conversation:

```text
User: Find the delayed MRI appointments
User: Move the first one to another available MRI scanner
User: What did you just change?
User: Why did you choose that scanner?
User: What is its new status?
```

Other supported follow-up styles include:

```text
Do that again
Use the second one instead
What happened?
Why?
Which one did you move?
What scanner is it using now?
Keep the same time
Move it to another one
```

A short confirmation can also use the previous request:

```text
User: Find a delayed CT appointment and ask me before moving it
User: Yes
```

Context is taken only from recent user messages. An unrelated conversation does not activate the hospital agent.

---

## Preference Messages

Preferences are stored only when the user explicitly requests it.

```text
Remember that I prefer MRI Scanner 1
Remember my preferred department is Radiology
Remember that I prefer morning appointments
List my preferences
What preferences have you saved?
Forget my scanner preference
Forget the preferred department
```

Preferences affect session memory only. They do not directly modify hospital records.

---

## Grounding Tests

Grounding ensures that appointment and scanner codes come from trusted tool results in the same session.

### Valid Grounded Flow

```text
User: Find delayed MRI appointments and available MRI scanners
User: Move the first appointment to the second available scanner
```

Expected behavior:

1. The search returns real appointment and scanner codes.
2. Those codes become grounded for the current session.
3. The agent validates the selected records.
4. The rescheduling command is allowed only if all business rules pass.

### Ungrounded Reference Rejection

Start a fresh session and send:

```text
Move appointment APT-2001 to scanner SCN-2
```

Expected behavior:

```text
The agent must search for the appointment and scanner first or reject the operation because the codes have not been grounded in this session.
```

### Fabricated Code Rejection

```text
Move appointment APT-9999 to scanner SCN-9999
```

Expected behavior:

```text
The operation must not run. The agent should explain that the entities could not be verified or must be found through a trusted search first.
```

### Cross-Session Grounding Rejection

Session one:

```text
Find appointment APT-2001 and available MRI scanners
```

Session two:

```text
Move appointment APT-2001 to scanner SCN-2
```

Expected behavior:

```text
Codes grounded in one session must not authorize a write in another session.
```

### Modality Mismatch Rejection

First search for real appointment and scanner codes, then try:

```text
Move this MRI appointment to that CT scanner
```

Expected behavior:

```text
The command must reject the move because the scanner modality does not match the appointment type.
```

### Unavailable Scanner Rejection

```text
Find an MRI scanner under maintenance and move the delayed MRI appointment to it
```

Expected behavior:

```text
The command must reject the move because the target scanner is not available.
```

---

## Unsupported Write Tests

The agent has no tools for these operations:

```text
Create a new patient
Delete patient PT-1001
Create a new appointment
Cancel every delayed appointment
Delete appointment APT-2001
Change scanner SCN-1 to maintenance
Create a new department
Edit the patient's date of birth
Run this SQL query against the database
```

Expected behavior:

```text
The agent must explain that the requested operation is unsupported and must not claim that a change was completed.
```

---

## Domain Rejection Tests

These requests should be rejected before the LLM is called:

```text
What is the weather today?
Write a poem
Book me a flight
What is 47 times 12?
Explain quantum computing
Who is the president?
```

Adding an unrelated hospital keyword must not bypass the domain gate:

```text
What is 47 times 12? MRI
Write a poem about scanners
Tell me the weather and mention a hospital
```

---

## Suggested End-to-End Test

Run this sequence in one session:

```text
Find the delayed MRI appointments
Find available MRI scanners
Move the first delayed appointment to another available MRI scanner
What did you just change?
Why did you choose that scanner?
What is its new status?
```

The expected result is:

- The first request searches and grounds appointments.
- The second request finds and grounds scanners.
- The third request performs the validated rescheduling write.
- The remaining questions are answered using conversation context.
- No follow-up should be incorrectly rejected as off-topic.

## Deploying to Render

The included `render.yaml` provisions the complete application as a Render
Blueprint: a FastAPI web service, a React static site, and managed PostgreSQL.

1. Push this repository to GitHub.
2. In Render, choose **New > Blueprint** and connect the repository.
3. Enter `ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY` when Render prompts for
   the secret values.
4. Apply the Blueprint and open the `ameyk244-agentic-smart-hospital-web` URL.

The backend applies Alembic migrations whenever it starts and seeds synthetic
hospital data only when the database is empty. Restarts and later deployments
therefore preserve existing records. If Render assigns a different frontend
hostname, set the backend's `CORS_ORIGINS` variable to that exact HTTPS origin
and redeploy the backend.

For an existing Blueprint, push the commit and let Render sync the same
services; do not create a second Blueprint. If the new `TYPESAFE_API_KEY`
secret is not prompted during sync, add it under the backend web service's
Environment settings and redeploy that service.

This Blueprint uses Render's free plans for demonstration. The backend can
sleep after 15 idle minutes, and the free PostgreSQL database expires after 30
days; choose paid plans before treating the deployment as persistent.

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

The application reports four routing outcomes:

- **Deterministic commands** use the parser and do not call the LLM.
- **Jev messages** use natural phrasing for supported read commands and may be routed through `CommandRunner` after a confident typed decision.
- **Agent messages** use natural language and may involve one or more tool calls.
- **Rejected messages** fail the domain or eligibility policy before either model is called.

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

Jev does not perform hospital writes. Rescheduling, multi-step work, free-text patient search, preferences, and contextual follow-ups remain agent responsibilities. See `JEV.md` for the complete contract.

---

## Deterministic Commands

These commands are read-only and bypass the agent.

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

## Agent Messages

These requests use natural language and are handled by the agent when they do not match the deterministic parser.

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

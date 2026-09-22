# Smart Hospital Operations Rulebook

Hospital operations have two input paths but one shared execution layer:

```text
Exact command -> deterministic parser -> CommandRunner -> database
Normal message -> domain gate -> agent -> optional tool -> CommandRunner -> database
Off-topic message -> rejected before the agent
```

The parser is intentionally strict. Normal hospital-related messages are
supposed to fall through to the agent.

## 1. Deterministic Commands

These exact command shapes are handled without an LLM call. They are all
read-only, return `handled_by: deterministic`, and create no agent trace.

| Command | Access | Domain |
|---|---|---|
| `list departments` | READ | Departments |
| `list scanners` | READ | Scanners |
| `list scanners [mri|ct|xray]` | READ | Scanners |
| `list scanners [available|in use|maintenance]` | READ | Scanners |
| `list scanners [mri|ct|xray] [available|in use|maintenance]` | READ | Scanners |
| `show patient <name>` | READ | Patients |
| `show next appointment` | READ | Appointments |
| `show the next appointment` | READ | Appointments |
| `list delayed appointments` | READ | Appointments |
| `list delayed appointments [mri|ct|xray]` | READ | Appointments |

Examples:

```text
list departments
list scanners mri available
show patient David Davis
show the next appointment
list delayed appointments ct
```

Matching is case-insensitive and ignores surrounding whitespace, but it is not
fuzzy. For example, `show me the next appointment` goes to the agent.

## 2. Domain Operations

`CommandRunner` is the canonical execution layer used by both the parser and
agent tools. Business rules belong here, not in the LLM.

| Operation | Access | Domain | Purpose |
|---|---|---|---|
| `list_departments` | READ | Departments | List departments |
| `list_scanners` | READ | Scanners | Filter scanners by type or status |
| `get_scanner_availability` | READ | Scanners | Read one scanner's availability |
| `search_patients` | READ | Patients | Find patients by name |
| `list_patient_appointments` | READ | Patients/Appointments | List a patient's appointments |
| `show_next_appointment` | READ | Appointments | Read the next appointment |
| `search_appointments` | READ | Appointments | Filter appointments |
| `list_delayed_appointments` | READ | Appointments | List delayed appointments |
| `reassign_scanner` | WRITE | Appointments | Change scanner and optionally time |

`reassign_scanner` validates the appointment, scanner, modality, and scanner
availability before committing. The agent-facing name for this operation is
`reschedule_appointment`.

## 3. Agent Fallback

The agent handles normal hospital-related language that does not match the
strict parser grammar. Agent turns return `handled_by: agent` and use the LLM,
so they consume API tokens even when no tool is called.

### Agent read tools

| Tool | Domain | Purpose |
|---|---|---|
| `search_appointments` | Appointments | Search by status, type, patient, department, or scanner |
| `get_scanner_availability` | Scanners | Check a grounded scanner |
| `execute_command` | Shared command layer | Run one of the deterministic parser commands |
| `list_preferences` | Session memory | Read remembered preferences |

### Agent write tools

| Tool | Write target | Purpose |
|---|---|---|
| `reschedule_appointment` | Hospital data | Reassign a grounded appointment to a grounded scanner and optionally change its time |
| `remember_preference` | Session memory only | Store a preference |
| `forget_preference` | Session memory only | Delete a preference |

`reschedule_appointment` is the agent's only hospital-data write. The agent
cannot create or delete appointments, edit patients, change scanner status,
modify departments, or execute arbitrary SQL.

Example agent messages:

```text
How many delayed MRI appointments are there?
Which available MRI scanners could handle them?
Find the appointments assigned to SCN-3.
Move the first delayed MRI appointment to another available MRI scanner.
Remember that I prefer MRI Scanner 2.
What preferences do you remember?
```

## 4. Contextual Follow-Ups

Short follow-ups can reach the agent when the same session contains a recent,
explicit hospital request:

```text
yes
no
go ahead
What happened?
What did you just change?
Why did you choose that scanner?
What is its new status?
Do that again
the first one
```

The same phrases without hospital context are rejected. Reaching the agent does
not authorize a write: the agent must still select an allowed tool, and every
tool must pass grounding and domain validation.

## 5. Grounding Rules

Grounding prevents the agent from writing with invented or unseen entity codes.

1. An agent search or the agent's `execute_command` tool exposes real entity
   codes to the session. A direct deterministic chat command does not create
   agent grounding.
2. Exposed codes are stored as grounded entities for that session.
3. `reschedule_appointment` requires both the appointment code and scanner code
   to be grounded.
4. `get_scanner_availability` requires its scanner code to be grounded.
5. Grounding never carries into a new session.
6. Grounding proves that a code was observed; normal validation still checks
   existence, entity type, modality, and availability.

Valid sequence:

```text
Find delayed MRI appointments and available MRI scanners.
Move APT-2003 to SCN-1.
```

Expected rejection in a fresh session:

```text
Move appointment APT-9999 to scanner SCN-9999.
```

## 6. Routing Rules

| Input | Result | LLM cost |
|---|---|---|
| Exact parser command | `deterministic` | None |
| Normal hospital request | `agent` | Yes |
| Contextual follow-up after hospital context | `agent` | Yes |
| Empty, invalid, or off-topic request | `rejected` | None |
| Disconnected bypass such as `What is 47 times 12? MRI` | `rejected` | None |
| Creative ask naming a hospital thing, e.g. `tell me a joke about patients` | `rejected` | None |

Tool-call count is not model-call count. A tool-free agent answer normally uses
one model call. A tool action commonly uses one model call to request the tool
and another to interpret its result.

### How the domain gate decides "hospital request"

A message reaches the agent when at least one clause (split on `. ? ! , ;`
and on `and`/`but`) contains:

- a hospital word **and** a request word. Entity codes (`APT-2001`, `SCN-1`,
  `PT-1001`, `STF-3`, `RM-2`, `DEPT-RAD`) count as hospital words, e.g.
  `reschedule APT-2001 to SCN-1`, `tell me about the radiology department`,
  `compare MRI and CT delays`, `anything delayed on CT today?`.

A code still needs a request word: `delete APT-2001`, `cancel APT-2001` and
`mark SCN-4 as available` are rejected, because nothing in this system can do
them and they shouldn't cost a model call. A hospital word or code standing
alone in its own clause does not count either, which keeps
`What's 47 times 12? APT-2001` and `Tell me a joke, scanner` rejected.

A clause containing `joke`, `poem`, `haiku`, `song`, `riddle`, `essay`,
`recipe`, `weather`, `password`, `credentials` or `sql` never counts — but
another clause in the same message still can, so
`tell me about MRI delays and write a poem` reaches the agent, which answers
the MRI part and declines the poem.

Reaching the agent grants nothing. The gate only decides whether a model is
asked; grounding, `CommandRunner` validation and the tool list decide what can
actually happen. `reschedule APT-9999 to SCN-1` reaches the agent and is then
refused, because those codes were never grounded and don't exist.

These now reach the agent; before this fix the gate wrongly rejected them:

```text
reschedule APT-2001 to SCN-1
move APT-2001 to SCN-5
is SCN-1 free?
tell me about patient David Davis
tell me about the radiology department
give me a table of all scanners with their type and status
describe the cardiology department
anything delayed on CT today?
look up patient Anthony
help me with the MRI backlog
```

Known limit: a keyword gate cannot read intent. `what is 47 times 12 for the MRI
appointment?` passes, because "what" and "MRI appointment" share a clause and
"times" is also a real scheduling word. The agent's system prompt declines the
arithmetic, but that still costs one model call.

## 7. Minimal Manual Check

Use this small set to avoid unnecessary API usage:

```text
list scanners mri available
Find the delayed MRI appointments
What did you find?
Move appointment APT-9999 to scanner SCN-9999
What is 47 times 12?
```

Expected results:

| Message | Expected handler | Expected behavior |
|---|---|---|
| `list scanners mri available` | deterministic | Read result, empty trace |
| `Find the delayed MRI appointments` | agent | Appointment search tool |
| `What did you find?` | agent | Contextual answer, usually no tool |
| Fake appointment/scanner codes | agent | Model refusal or grounding rejection, no write |
| Arithmetic question | rejected | No model call |

Only test a successful reschedule when needed because it changes persistent
hospital data. Starting a new chat creates a new session but does not undo
database changes.

## 8. Seed Reference

After a clean reseed:

- Departments: `DEPT-CARD`, `DEPT-ER`, `DEPT-ORTH`, `DEPT-RAD`
- Available MRI scanners: `SCN-1`, `SCN-2`
- MRI in use: `SCN-3`
- MRI maintenance: `SCN-4`
- Available CT scanners: `SCN-5`, `SCN-6`
- CT in use: `SCN-7`
- Available XRAY scanner: `SCN-8`
- Delayed MRI appointments: `APT-2001` through `APT-2004`
- Delayed CT appointments: `APT-2005` through `APT-2007`

Rescheduling changes this data, so later results may differ from the clean seed.

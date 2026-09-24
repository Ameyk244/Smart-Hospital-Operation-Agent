# Smart Hospital Operations Rulebook

Hospital operations have four routing outcomes and one shared execution layer:

```text
Exact command  -> deterministic parser -> CommandRunner -> database
Near-miss read -> domain + eligibility gates -> Jev -> CommandRunner -> database
Normal message -> domain + eligibility gates -> agent -> optional tool -> database
Off-topic msg  -> rejected before either model
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

Matching is case-insensitive, collapses repeated whitespace, and ignores
trailing punctuation (`.` `?` `!` `,` `;` `:` `…`), but it is not fuzzy. So
`list departments.` and `show patient David Davis?` match; `show me the next
appointment` still goes to the agent. Only *trailing* punctuation is dropped —
an apostrophe or hyphen inside a name (`O'Neil-Smith`) is kept.

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
| Near-miss phrasing of a supported read command | `jev` | Jev call; no Claude call |

Tool-call count is not model-call count. A tool-free agent answer normally uses
one model call. A tool action commonly uses one model call to request the tool
and another to interpret its result.

### The `jev` path

Jev is on `master` and enabled by default (`ENABLE_JEV_FAST_PATH=true`). A
message that misses the strict
parser is shown to a typed-decision model, which picks which known command
it maps to — or `none`. A confident match (>= 0.9) runs through the same
`CommandRunner` as an exact parser match, so a near-miss like
`can you pull up the department list?` can be answered with no Claude call
at all. It shows a `jev` badge and writes a `jev_invoked` trace row.

It sits **after** the domain gate, so off-topic messages are still rejected
for free and never reach it. It cannot reach anything `CommandRunner` won't
already accept, it never invents a command argument, and it never executes
`search_patients` (a fixed choice can't produce a free-text name). Anything
unconfident, unrecognised, or failing falls through to the agent unchanged —
so the worst case is the behavior you already had. A `jev_invoked` row is
written even when it declines, so the trace shows it was consulted.

Every read/write boundary above is unchanged: this path adds no new way to
mutate data, and the only hospital-data write in the system is still
`reschedule_appointment`, which the agent alone can call.

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

## 9. Message Bank — which path actually handles what

Every routing result below was **measured, not guessed**: each message was run
through the real parser, the real domain gate, and (where it got that far) a
real Jev call on `jev-1.13.0`. Confidence figures are what Jev actually
returned. Jev results assume `ENABLE_JEV_FAST_PATH=true`; with the flag off,
every `jev` row below becomes an `agent` row instead.

Jev is non-deterministic, so confidences will wobble a little run to run.
Anything in the 0.85–0.95 band can land on either side of the 0.9 threshold.

### 9.1 Deterministic — exact grammar, no model call, no cost

```text
list departments
list scanners
list scanners mri
list scanners ct
list scanners xray
list scanners available
list scanners mri available
list scanners ct available
list scanners mri maintenance
list scanners in use
show patient David Davis
show patient Anthony
show next appointment
show the next appointment
list delayed appointments
list delayed appointments mri
list delayed appointments ct
LIST DEPARTMENTS
   list departments
```

Case, extra whitespace and trailing punctuation don't matter; nothing else
does. Add a please, a "me", or an "all" and you leave this path entirely.

### 9.2 Jev fast path — parser missed, Jev recognised it anyway

Verified matches, with the confidence Jev returned and the command it built:

```text
show me all the departments                     0.98  list_departments
which appointments are running late?            0.99  list_delayed_appointments
how many delayed MRI appointments are there?    0.97  list_delayed_appointments {appointment_type: MRI}
which MRI scanners are available?               0.96  list_scanners {type: MRI, status: AVAILABLE}
can you pull up the department list for me?     0.95  list_departments
what departments do you have?                   0.95  list_departments
are any CT scanners free right now?             0.95  list_scanners {type: CT, status: AVAILABLE}
which departments exist here?                   0.93  list_departments
what scanners do we have?                       0.90  list_scanners
```

Note the argument extraction: "which MRI scanners are available?" becomes
`list_scanners` with **both** a type and a status filter, pulled from separate
`Choice` questions in the same call. A filter is only applied when it clears
the same 0.9 threshold — otherwise it's omitted, never guessed, so you get a
wider-but-correct answer rather than a confidently wrong one.

**Near-misses that fell just short of 0.9 and went to the agent instead:**

```text
what's the next appointment?                    0.87  show_next_appointment
when is the next appointment scheduled?         0.84  show_next_appointment
any scanners under maintenance?                 0.84  list_scanners
```

These are exactly the phrasings the fast path is *meant* to catch, and the
0.9 threshold is what stops them. That threshold is this project's
conservative policy because a match executes a real command; it is not a
universal cutoff prescribed by the provider.
Lowering `JEV_CONFIDENCE_THRESHOLD` to 0.8 would capture all three — at the
cost of acting on weaker evidence. It's a genuine trade, not an oversight.

### 9.3 Agent — Jev correctly declines, or the request genuinely needs it

Verified: Jev returned `none` with high confidence on each of these, i.e. it
actively recognised these as not-a-known-command rather than failing to
understand them.

```text
move the first delayed MRI appointment to an available scanner   none @ 1.00
what do I prefer?                                                none @ 1.00
find delayed MRI appointments and then move the first one        none @ 0.99
remember that I prefer MRI scanners                              none @ 0.99
is radiology or cardiology more backed up?                       none @ 0.82
```

The first and third are the important ones: a **write** request and a
**multi-step** request both declined rather than being mangled onto a
read-only command. That's the safety property, working.

Other things that always need the agent:

```text
forget my scanner preference
what did you just change?
why did you choose that scanner?
compare MRI and CT delays
which patients have appointments on a maintenance scanner?
give me a table of all scanners with their type and status
```

Patient lookups by name reach the agent even though `search_patients` exists
as a deterministic command: a fixed `Choice` cannot produce a free-text name,
so the fast path recognises the intent but deliberately refuses to execute it
and hands off to the agent's real search tool.

### 9.4 Rejected — never reaches any model

```text
what's the weather like today?
write me a poem about the ocean
what is 47 times 12?
book me a flight to Chicago
tell me a joke
how are you?
What's 47 times 12? mri          <- disconnected-keyword bypass, still rejected
Write me a poem. patient         <- same shape
```

### 9.5 Formerly rejected — now fixed

These hospital requests used to be **wrongly rejected** by the domain gate. They
now reach the agent (or, with the flag on, Jev first), because entity codes
count as hospital words and common request verbs (`tell`, `give`, `describe`,
`compare`, `look`, `help`, `anything`, ...) are recognised:

```text
reschedule APT-2001 to SCN-1
move APT-2001 to SCN-5
tell me about patient David Davis
tell me about the radiology department
anything delayed on CT today?
what imaging machines do we have?
```

The fix did not loosen safety. Requests nothing in this system can do stay
rejected — `delete APT-2001`, `cancel APT-2001`, `mark SCN-4 as available` —
and so do credential and SQL asks. See §6's "How the domain gate decides" for
the full rule, and `docs/PROGRESS.md` for the old-vs-new safety comparison.

`Move appointment APT-9999 to scanner SCN-9999`, the grounding example in §5
and §7, was never affected: it always contained "appointment" and "scanner",
reached the agent, and was stopped by grounding. (An earlier version of this
section wrongly said it was blocked by the gate.)

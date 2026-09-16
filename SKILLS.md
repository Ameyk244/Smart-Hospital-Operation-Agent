# How this project uses subagents

This is a learning project about agent architecture, so the meta-process of
building it is part of the material. This file records how delegation was
actually used (updated as it happens, not reconstructed after the fact).

## Principles followed

- The lead session (Claude Code, driven by the master prompt in the original
  conversation) remains the coordinating agent throughout. It owns
  architecture decisions, phase sequencing, commit boundaries, and
  integration of anything a subagent produces.
- Subagents are used for **clearly separated, independently verifiable**
  chunks of work — not for splitting a single tightly-coupled module across
  multiple agents, which would risk conflicting edits and incoherent style.
- Every subagent's output is reviewed by the lead session before being
  committed. "The subagent said it works" is never sufficient — its
  diff/output is read and, where applicable, its tests are run.
- Parallel delegation is only used when the tasks touch disjoint files.

## Delegation log

| Phase | Task | Delegated? | Why |
|---|---|---|---|
| 0-4 (foundation, schema, seed data, deterministic parser, CommandRunner, thin vertical slice) | Architecture, schema, repositories, parser, execution layer | No — done directly by the lead session | This is the critical path everything else depends on; correctness and internal coherence matter more here than parallel throughput, and the pieces are too interdependent to safely split. |
| Later phases (frontend, targeted test-suite expansion, focused audits) | TBD | TBD | Recorded here as it happens. |

This table will grow as the project proceeds past the initial vertical
slice. See `docs/PROGRESS.md` for the actual implementation timeline.

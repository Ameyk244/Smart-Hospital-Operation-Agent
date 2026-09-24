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
| 0-9 (foundation through checkpointing: schema, seed data, deterministic parser, CommandRunner, LLM provider, tools, grounding, memory, checkpointing, and the operations/trace API surface) | Everything backend | No — done directly by the lead session | This is the critical path everything else depends on; correctness and internal coherence matter more here than parallel throughput, and the pieces are too interdependent to safely split. |
| 10 (frontend: operations view, chat panel, trace panel) | React/TypeScript UI build | **Yes** — one `general-purpose` subagent, run in the background | By this point the API surface (`/api/operations/*`, `/api/chat`, `/api/sessions/{id}/{trace,messages}`) was stable and tested (72 backend tests passing). Frontend work has no coupling to backend implementation details, only to the API contract — exactly the "clearly separated, independently verifiable" case these principles call for. Given a complete brief (exact endpoint shapes, required 3-panel layout, explicit instruction that the trace panel must poll the real trace endpoint rather than being mocked), it could build and self-verify the whole thing without the lead session's involvement mid-build. |
| 11 (five UI/UX fixes: Markdown rendering, badge restore, live progress indicator, Operations-panel row highlighting, session lifecycle) | Task 4 (row highlighting) and Tasks 1+2+3+5 (Markdown/badge/progress/session, all in `ChatPanel.tsx`) | **Yes** — two `general-purpose` subagents, run in parallel | Task 4 and the ChatPanel bundle touch disjoint files, so they qualified for parallel dispatch under the "only when tasks touch disjoint files" rule. A third subagent originally planned for Task 5 alone was folded into the ChatPanel bundle *before* dispatch, not after a collision — the initial plan assumed session-storage logic was isolated from chat rendering, but `ChatPanel.tsx` already owned the session id, the message list, and the fetch calls, so a standalone Task-5 subagent would have edited the same file as the Task-1/2/3 one. Shared groundwork (`useTrace` hook, `touchedEntityCodes` plumbing, Vitest setup) was done directly first so neither subagent had to invent infrastructure the other also needed. |
| 11 follow-up (delegation-safety research) | Determine how Claude Code can enforce a frontend-only subagent boundary | **Yes** - one `claude-code-guide` research subagent | This was research-only, not a coding task. Its findings were cross-checked before the restricted `frontend-worker` type was added. |
| Domain-relevance gate | Implement the backend domain gate and its tests | **Yes** - one tightly scoped `general-purpose` coding subagent | The feature crossed the route, gate, tests, and architecture docs as one cohesive backend unit. Its diff and full offline suite were independently reviewed before commit. |
| Jev fast-path widening (developed on `jev-testing`, now merged) | (a) backend routing + Jev client + tests; (b) frontend `jev` badge, `jev_invoked` trace rendering, cost-comparison page + tests | **Yes** - two subagents in parallel: one `general-purpose` (backend), one `frontend-worker` (frontend) | Parallel was only safe here *because the lead session settled the backend/frontend contract first* rather than letting the two agents negotiate it — the `handled_by: "jev"` value, the exact `jev_invoked` event shape (`event_type`/`tool_name`/`status`/`latency_ms`/`error_category` and all seven `arguments_json` keys), and the `GET /api/cost-comparison` response shape were all researched, fixed, and written verbatim into *both* briefs before dispatch. With the contract frozen, the file boundaries were genuinely disjoint (`backend/` vs `frontend/`) and neither agent had to guess at the other's output. The frontend half went to the restricted `frontend-worker` type (no `Bash`), so `npm run build`/`lint`/`test` were run by the lead session, not the subagent — the intended workflow for that type rather than a limitation worked around. |
| Voice input, Phase 3 (branch `voice`, WebSocket + energy-based VAD + STT-to-routing wiring) | The WebSocket endpoint, its VAD state machine, per-utterance DB session handling, and the three failure modes (connection drop, STT error, max-duration cutoff) flagged in the pre-build audit | **Yes** - one `general-purpose` backend-scoped subagent, not parallelized with Phase 4 | Phases 1-2 (the shared `handle_chat_message()` extraction and the fixed-file STT proof) were done directly by the lead session, sequentially, since Phase 2 depended on Phase 1 being verifiably correct first. Phase 3 was substantial enough on its own to delegate, but Phase 4 (frontend mic control) is explicitly withheld until this phase's WebSocket message contract — fixed and written into this subagent's brief before dispatch, the same discipline as the Jev backend/frontend split above — is confirmed correct by review, not just declared stable in a brief. |

**Recorded total:** eight subagent runs: seven coding subagents and one
research-only subagent. The coordinating Claude Code lead session is not
counted as a subagent run.

**Incident from phase 11, and why it changed how delegation works going
forward**: the subagent scoped to Tasks 1/2/3 (text-instructed to stay in
`ChatPanel.tsx`, no stated reason to touch the backend) left an untracked
Alembic migration in the repo and applied it to the live dev database,
which dropped LangGraph's own checkpoint tables in the process (fully
remediated — see `docs/PROGRESS.md`'s incident section for the fix and the
explicit no-data-loss verification). Attribution is **not conclusively
attributed, partially corroborated for A** via recovered git stash
objects — not the same as clearing that subagent. The root cause was that
a prompt-level "stay in frontend" instruction is not an enforced boundary
when the agent still has `Bash`. The concrete fix, not just stronger
wording: a new `.claude/agents/frontend-worker.md` subagent type with no
`Bash` tool at all, confirmed (via research into Claude Code's actual
subagent tooling) to be the only real access-control mechanism available —
the generic `Agent` tool has no allow/deny list, `isolation: "worktree"`
only isolates the file checkout, and Bash deny-rules in `settings.json` are
explicitly documented as not a security boundary. Until every
frontend-only task is dispatched through that narrower type, diffing
`backend/` after any subagent run — regardless of its stated scope — is the
standing compensating control.

**How the frontend delegation was reviewed before committing** (not just
"the subagent said it passed"): re-ran `npm run build` independently (clean,
zero errors); started both the backend and frontend dev servers fresh in
this session (not reusing the subagent's own run); fetched the dev server's
HTML directly; hit every `/api/operations/*` endpoint the operations panels
depend on and confirmed the response shapes matched `frontend/src/api/
types.ts` field-for-field; sent a real chat message through `/api/chat` and
confirmed the trace endpoint correctly returned an empty list (deterministic
path) matching what `TracePanel.tsx`'s empty-state logic expects; read
through `App.tsx`, `ChatPanel.tsx`, `TracePanel.tsx`, and `client.ts` in
full before committing. The subagent's own report also documented
independent verification (Playwright-driven browser screenshots against the
live agent path, confirming a real grounded tool-call trace rendered
correctly) — its devDependency was removed again before handing back, and
that was checked too (`package.json` has no leftover test tooling).

This table will grow as the project proceeds past this point. See
`docs/PROGRESS.md` for the actual implementation timeline.

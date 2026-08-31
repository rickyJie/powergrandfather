# ADR-0004: Multi-agent CLI-adapter layer

Status: Accepted (2026-07-25)

## Context

The initial codex integration (branch `llj_dev_codex`, milestones
P0–P7) added codex as a **second backend** bolted on next to claude —
a `Session.backend` enum, a `codex_rollout_path` column, a parallel
`CodexRolloutTailer`, a `derive_codex_events` mapper. Every downstream
subsystem (EventStream, Hooks, WorkflowOrchestrator, SupervisorAgent,
AgentDeck, Frontend) needed its own `if backend == codex:` branch.

Adding a third CLI (e.g. gemini) would triple those branches. That
scales linearly with adapter count and breaks the
"single-responsibility-per-file" property that made CSM's code review
tractable.

## Decision

Introduce a first-class `CLIAdapter` abstraction under
`csm/backends/`. Every CLI-specific responsibility (argv construction,
session-id lifecycle, artifact tailing, event derivation, hook config,
environment paths) lives inside a single adapter module. Domain
services (SessionManager, EventStream, Workflow, Supervisor,
AgentDeck) talk to the abstraction via the `AdapterRegistry` and
never special-case on adapter name.

Enforcement:

- Grep-lint CI script (`scripts/lint-agent-abstraction.sh`) fails any
  build where `if agent == "..."` / `backend == "..."` appears outside
  the transitional allowlist (`csm/backends/`, tests, migrations, one
  legacy path in `session_manager/manager.py`).
- `Session.agent` is a free-form string (not an enum) — the set of
  registered adapters is open at compile time and env-gated at runtime
  via `CSM_ENABLE_<name>=1`.

Additional coupled decisions:

- **User preference is a first-class model.** `UserPreference`
  (single-row table) records the default adapter and (optional)
  supervisor override. Frontend first-run wizard writes this on user
  choice.
- **Resolution chain is a single pure function.**
  `resolve_agent(explicit, context_default, user_default)` is the ONLY
  place per-call resolution happens. Invalid `explicit` raises
  `UnknownAgentError` → HTTP 400 (silent fall-through was rejected
  per backend-engineer P1 review).
- **Session-id lifecycle is two hooks, not one.** Claude bakes id
  pre-spawn via `--session-id`; codex discovers post-spawn from the
  rollout file. Compressing this into one method leaves both
  implementations unclear when to call and what None means.
- **Event derivation is per-adapter, canonical downstream.** Adapters
  translate their native records to `csm.core.events.Event`. Adapter-
  specific signals go in `Event.payload` under `_<agent>_*` prefix
  (see `docs/backends/canonical_events.md`).
- **EventStream multi-adapter loop is concurrent + isolated.**
  `_tick_once` fans out `scan_events()` across adapters via
  `asyncio.gather(..., return_exceptions=True)` so one slow / failing
  adapter can't block the others (backend-engineer P0 review).

## Consequences

**+** Adding a new adapter is a self-contained change: one new
directory under `csm/backends/`, one line in `build_default_registry`,
one entry in the sandbox script. No domain-code edits.

**+** Downstream code stays backend-agnostic. Reading a Session row's
`agent` field remains legal (it's a data attribute); switching on it
in control flow is a lint failure.

**+** Frontend flow tightens: users pick a default once via wizard,
override per-session via dropdown, no hidden state.

**-** More indirection for the two adapters we have today. Reading
"how does session creation work" now takes one extra hop through
`AdapterRegistry.get(agent).build_argv(...)`. Justified by the linear
scaling as more adapters land.

**-** Some legacy paths remain during transition (marked in code):
`session_manager/manager.py` still has if-branches under an
`adapter_registry=None` fallback for tests that construct
SessionManager directly. `event_stream.py` similarly. Both are
tolerated by the CI lint's allowlist. M6+ cleanup should delete them
once every test wires a registry.

## Rejected alternatives

**Entry-point-based plugin discovery.** Rejected — CSM is a
single-user local app, not an enterprise plugin marketplace. Static
imports keep the loading path visible in one file.

**Discriminated events (backend field on Event, downstream switches).**
Rejected — N agents means N-way switch statements in every consumer.
Canonical events cost a mapping layer once (in the adapter) but keep
downstream simple.

**Backwards-compatibility permanent aliases** (`backend` field on API
forever). Rejected — 1-release deprecation window is enough for a
single-user local tool with no external clients. Longer keeps the
codebase carrying dead names.

## Files touched by the refactor

- New: `backend/csm/backends/{base,registry,resolver,errors,__init__}.py`
- New: `backend/csm/backends/claude/{adapter,events,hooks}.py`
- New: `backend/csm/backends/codex/adapter.py`
- New: `backend/csm/models/user_preference.py`
- New: `backend/csm/api/{backends,preferences}.py`
- New: `frontend/src/api/{backends,preferences}.ts`
- New: `frontend/src/stores/{backends,preferences}.ts`
- New: `frontend/src/components/{AgentBadge,AgentSelector,FirstRunWizard}.vue`
- New: `frontend/src/views/Settings.vue`
- New: `alembic/versions/u3o5j7k8lhim_multi_agent_v2.py`
- New: `scripts/lint-agent-abstraction.sh`
- New: `docs/backends/{canonical_events,adding_a_new_adapter}.md`
- Modified: `backend/csm/models/{session,file_state,__init__}.py`
- Modified: `backend/csm/core/event_stream.py`
- Modified: `backend/csm/modules/session_manager/manager.py`
- Modified: `backend/csm/api/sessions.py`
- Modified: `backend/csm/main.py`
- Modified: `frontend/src/App.vue`, `router.ts`
- Modified: `alembic/env.py` (test-friendly URL override; loggers preserved)

## References

- Product-manager review: (in-conversation report, 2026-07-25) —
  see `is_first_run` wizard + AgentBadge always visible.
- Backend-engineer review: `/tmp/backend-review-2026-07-25-203751.md`
  — the P0/P1/P2 items whose remediations shaped this ADR.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project at a glance

PowerGrandFather / CSM (Claude Session Manager) is a **single-user local web console** for managing tmux-style parallel Claude Code sessions, automation tasks, token usage, and listening ports. The deployment shape is one FastAPI process + one Vue 3 SPA + one SQLite file. No Redis, no Postgres, no message broker — see `docs/decisions/0002-single-process-monolith.md`.

The backend package lives at `backend/csm/` (note: `pyproject.toml` sets `where=["backend"]`, so imports are `csm.*` even though the source root is `backend/`). Frontend is `frontend/`. Built SPA goes to `frontend/dist/` and is served by uvicorn in prod mode.

## Commands

All backend commands assume the `csm` conda env (Python 3.11) is active.

```bash
conda activate csm
# 内部 devpi 缺 pytest-asyncio / mypy 之类的公共 dev 包,走清华源兜底
pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple

# DB migrations (SQLite at csm.db in project root by default)
alembic upgrade head
alembic revision -m "..."      # then edit alembic/versions/<rev>.py

# Frontend
cd frontend && npm install && npm run build && cd ..

# Run — production (single uvicorn serving API + frontend/dist)
./scripts/start.sh             # 127.0.0.1:8000, writes csm.pid + csm.log
./scripts/start.sh HOST PORT   # custom bind (default 127.0.0.1:8000 — loopback only)
./scripts/stop.sh              # SIGINT → SIGTERM → SIGKILL with 10s grace

# Run — dev (uvicorn --reload on :8000 + vite on :5173, vite proxies /api)
./scripts/dev.sh

# Tests
pytest                                                       # full suite
pytest tests/unit                                            # unit only
pytest tests/integration                                     # integration
pytest tests/unit/test_event_stream.py                       # single file
pytest tests/unit/test_event_stream.py::test_specific_name   # single test
pytest -k "token and not rollup"                             # keyword filter

# The mobile suites are NOT in `testpaths`, so a bare `pytest` skips them
# entirely. Run them explicitly when touching anything they cover — the
# session WS / message POST contract in particular.
pytest mobile/tests/backend                                  # mobile backend
cd mobile/frontend && npx vitest run && cd ../..             # mobile frontend

# Lint
ruff check .
ruff format .
```

When running anything that would `spawn claude` (automation launches, M1 session creation tests), set `CSM_CLAUDE_ARGV='bash -i'` to substitute bash for the real `claude` CLI — otherwise you'll burn real Claude tokens on every test.

## Architecture

### Single FastAPI process, lifespan-managed

`backend/csm/main.py` is the entry point. The `lifespan()` context manager constructs all subsystems in a specific order and tears them down in reverse:

```
EventStream → SessionManager (reap orphans) → TokenAggregator → AlertEvaluator
→ BudgetEvaluator → NotificationBus → AgentStore/ConvManager
→ AutomationRunner → WorkflowLoader → WorkflowOrchestrator
→ AutomationScheduler (started last, wired to orchestrator) → SupervisorAgent
→ RollupWorker
```

**Post-P0-P4 consolidation (2026-07-06)**: M4 TaskDefinition was retired.
Automation now has only two concepts: **workflow** (YAML template) and
**mission** (concrete execution). Stage-level execution records live in
the `stage_execution` table (Python class still `Run` for compat; alias
`StageExecution` also exported).

Everything attaches to `app.state.*` so API routes can reach it via `request.app.state`. **Do not introduce module-level singletons** for new subsystems; follow the lifespan pattern.

### EventStream is the spine

`backend/csm/core/event_stream.py` tails `~/.claude/projects/**/*.jsonl` every 5s and publishes typed events (session.started, usage.recorded, message.assistant_done, etc.). It is an **in-memory pub/sub bus** — events are not persisted by EventStream itself. Consumers (`NotificationBus`, `TokenAggregator`, `AutomationRunner`, `SupervisorAgent`) subscribe and persist what they care about.

When adding a new event type, edit `core/events.py` (enum + payload) and `core/event_stream.py` (derivation logic), then update subscribers.

### Module dependency graph

```
M2 EventStream      (foundation, no deps)
  ├─ M3 NotificationBus    (subscribes; routes to InAppSink + optional LarkSink)
  ├─ M5 Token (aggregator/alert/budget/rollup)
  ├─ AutomationRunner      (subscribes to terminal events → finalizes stage runs)
  ├─ M8 WorkflowOrchestrator (missions state machine)
  └─ Supervisor agent      (subscribes for post-mission review)

M1 SessionManager   (forks PTY children; independent of M2-M6)
  └─ WorkflowOrchestrator uses M1 to spawn AUTO sessions per claude stage
  └─ AutomationScheduler (cron) uses orchestrator to launch missions on tick

Workflow authoring (P1, 2026-07-06):
  POST /api/workflows/generate → csm.modules.workflow.authoring.generator
  spawns `claude -p` in the user's target repo, splices in
  `docs/workflow_authoring_guide.md` +
  requirement, then runs R9-R19 review on the emitted YAML.

```

### Module map (where to change what)

| Concern | Location |
|---|---|
| HTTP routes | `backend/csm/api/*.py` (one file per module) |
| ORM models | `backend/csm/models/*.py` |
| Business logic | `backend/csm/modules/{session_manager,automation,workflow,token,agent,supervisor}/` |
| Workflow authoring | `backend/csm/modules/workflow/authoring/` (generator + server-side prompt) |
| External-world adapters | `backend/csm/adapters/` (claude_subprocess, jsonl_tail, inapp_sink, lark_sink) |
| Frontend views | `frontend/src/views/*.vue` (one per module) — Sessions / Tokens / Budgets / AutomationRuns / AgentDeck / AgentChat |
| API clients | `frontend/src/api/*.ts` (mirrors backend routers) |
| Workflow YAMLs | `tasks/*.workflow.yaml` — POST `/api/workflows/reload` after edits (or `+ New workflow` in UI) |
| Authoring guide | `docs/workflow_authoring_guide.md` |
| Global config | `backend/csm/config.py` (Pydantic settings, `CSM_*` env prefix) |
| Migrations | `alembic/versions/` |

### Session abstraction

`Session.type` is the discriminator for what a row represents: `interactive` (user-driven PTY), `auto` (spawned by AutomationRunner), `chat_agent` (Agent Deck conversations). All three are the same `Session` rows with the same lifecycle hooks — don't add parallel tables for new session kinds. (`onboarding_agent` / `supervisor_agent` were reserved in v1 but never constructed and were retired 2026-07-25; both are deferred to v2 per `docs/known_issues.md`.)

`Session.claude_session_id` (the JSONL uuid) ↔ `Session.id` (CSM row id) reconciliation is **only partial in v1**: SessionManager fork-side metadata and JSONL-side detection are not fully wired. Don't assume every row has a `claude_session_id`.

## Non-obvious gotchas

These will burn you if you don't know them:

- **Loopback bind is the default.** `settings.host` defaults to `127.0.0.1` and `scripts/start.sh` / `scripts/dev.sh` bind `127.0.0.1:8000`, so the console is reachable only from the host itself — reach it over an SSH tunnel (VSCode port-forward or `ssh -L`). Pass a HOST arg (`./scripts/start.sh 0.0.0.0`) or set `CSM_HOST=0.0.0.0` to expose it on the LAN. `/api/sessions` accepts `argv` overrides, so if you do bind `0.0.0.0`, anyone on the LAN can spawn arbitrary processes — set `CSM_ACCESS_TOKEN` and never do it on an untrusted network. `POST /api/sessions` refuses non-`claude` argv by default; set `CSM_ALLOW_ARBITRARY_ARGV=1` for test / dev spawn.
- **Enum wire format is lowercase strings.** All API enum fields serialize as `Enum.value` (lowercase). The full table is in `README.md` under "API wire format reference".
- **Token alerts retired the v1 hardcoded model (2026-07-10).** `AlertRule` / `/api/tokens/alert-rules/*` / `AlertEvaluator` are all gone. Replaced by `AgentAlertRule` — user writes NL, agent generates a Python check script (subprocess-sandboxed at each tick), optional `escalate: true` calls `claude -p` again on fire for a root-cause + recommendations body. Endpoints: `/api/tokens/agent-alerts` + `/api/tokens/agent-alerts/generate` for the two-step authoring flow.
- **P2 retired M4 TaskDefinition (2026-07-06).** `/api/tasks/*` endpoints, `+ New task` button, `TaskList.vue`, `task_loader.py`, `review/service.py` are all gone. Automation is workflow-only now. `ScheduleEntry.task_def_id` and `Run.task_def_id` columns have been dropped; `run` table renamed to `stage_execution`. Any code path assuming M4 will fail loudly (`NotImplementedError` or missing endpoint).
- **DELETE `/api/sessions/{sid}` blocks up to 15s** (SIGINT 5s → SIGTERM 5s → SIGKILL 5s). HTTP clients need `timeout >= 20s`.
- **EventStream does not persist.** If a feature needs replay across restarts, the consumer must own the persistence.
- **SQLite is the only datastore.** Don't introduce Redis / Postgres / external queues — see ADR-0002.
- **Backend imports use `csm.*` even though the source lives in `backend/`.** This is configured in `pyproject.toml [tool.setuptools.packages.find]`. Run uvicorn as `uvicorn csm.main:app` (with `pip install -e .` having put `backend/` on sys.path), not as `uvicorn backend.csm.main:app` unless you're in a debugging context where editable install isn't active.
- **Production mode requires `frontend/dist/`.** `main.py` only mounts the SPA fallback if `frontend/dist/index.html` exists. If only the API works in prod, rebuild the frontend.
- **datetimes have no timezone info in API responses.** Treat naked `2026-06-24T...` strings as UTC.
- **No quota-percentage metric.** Token UI shows absolute counts only; see ADR-0001 for why. Don't add a `quota_pct` field without revisiting that decision.
- **A skill is a directory, not a file (bundle sync, 2026-08-30).** `Skill.body_md` is only `SKILL.md`; every sibling (`query.py`, `references/*.md`, `scripts/*.py`) lives in the `skill_file` table and is materialised by `adapter.write_skill_bundle()`. Three traps: (1) **permission bits must be re-applied on every write** — `atomic_write_with_hash_guard` makes its temp file 0600, so without the explicit `os.chmod` a helper script arrives non-executable, which breaks the skill just as thoroughly as omitting it; (2) **pruning is scoped to `Skill.last_synced_files[agent]`**, never "everything not in the new bundle", so hand-placed files survive; (3) **symlinked skill dirs are refused, not followed** — in a real `~/.claude/skills` most entries are symlinks into a skill-book git repo, and `os.replace`/`rmtree` would silently edit that working tree, so the adapter raises `ExternalSkillSource` → `DriftReason.EXTERNAL_SOURCE` → `SKIPPED`. Exclusions are hard-coded junk (`__pycache__`, `.git`, `*.pyc`, …) plus an optional per-skill `.csmsyncignore` (gitignore syntax); caps are 1 MiB/file and 200 files/skill, and blowing either is **reported**, never silently truncated. `write_simple_skill` is deprecated — it writes SKILL.md and nothing else. Rows created before this feature carry no bundle: repair them with `POST /api/sync/skills/reingest?agent=claude` (or "Repair bundles" in `Settings > Sync`).

## Reference docs

| File | When to read |
|---|---|
| `README.md` | Feature surface + API enum wire format table |
| `docs/architecture.md` | Module DAG, data flows, lifespan order |
| `docs/USAGE.md` | Verified curl recipes per module |
| `docs/known_issues.md` | v2 deferrals + caveats |
| `docs/decisions/` | ADRs — read before challenging an architectural choice |

# PowerGrandFather — Architecture

_Last aligned with code: 2026-07-10 (post commit 918de63)._

## Single-process monolith

```
══════════════════════════════════════════════════════════════════════════════════
                             USER  &  EXTERNAL CONSUMERS
══════════════════════════════════════════════════════════════════════════════════

   ┌────────────────────────────────────┐                   ┌────────────────┐
   │     Web UI (Vue 3 + xterm.js SPA)  │                   │  Claude Code   │
   │ ┌──────────┬────────────┬───────┐  │                   │ / user browser │
   │ │ Sessions │ AgentDeck  │ Chat  │  │                   │  hits /api     │
   │ ├──────────┼────────────┼───────┤  │                   │                │
   │ │ Tokens   │ Budgets    │ Auto  │  │                   │                │
   │ ├──────────┼────────────┴───────┤  │                   │                │
   │ │ Settings │ Notifications      │  │                   │                │
   │ │          │                    │  │                   │                │
   │ └──────────┴────────────────────┘  │                   │                │
   └─────────────────┬──────────────────┘                   └────────┬───────┘
                     │ HTTP + WebSocket                              │ HTTP
                     │                                               │ /api/…
                     ▼                                               ▼
══════════════════════════════════════════════════════════════════════════════════
                  BACKEND SERVICE (single FastAPI process · Python 3.11+)
══════════════════════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────────────────────────────────────┐
  │  ┌──────────────────────────┐    ┌─────────────────────────────────────┐  │
  │  │   M1 SessionManager      │    │  Automation (workflow-only)         │  │
  │  │  • fork claude CLI PTY   │◄───┤  ┌──────────────────────────────┐  │  │
  │  │  • WebSocket I/O fan-out │    │  │ WorkflowLoader (YAML files)  │  │  │
  │  │  • Ring buffer (1 MB)    │    │  │ AutomationScheduler (cron)   │  │  │
  │  │  • startup_reap_orphans  │    │  │ WorkflowOrchestrator         │  │  │
  │  │  • claude_session_id     │    │  │   ├─ launches missions        │  │  │
  │  │    partial UNIQUE (live) │    │  │   ├─ per-stage AUTO sessions │  │  │
  │  └────────────┬─────────────┘    │  │   ├─ SESSION_IDLE watchdog   │  │  │
  │               │ spawn PTY         │  │   └─ global_timeout rescuer  │  │  │
  │               ▼                   │  │ AutomationRunner (finalize)  │  │  │
  │     [Claude child processes]      │  │ Authoring: claude -p writer  │  │  │
  │               │                   │  └──────────────────────────────┘  │  │
  │               │ (writes JSONL)    └─────────────┬───────────────────────┘  │
  │               ▼                                 │                          │
  │  ╔═══════════════════════════════════════════════════════════════════════╗│
  │  ║   M2 EventStream (in-memory pub/sub — NOT persisted)                  ║│
  │  ║   Tails ~/.claude/projects/**/*.jsonl every 5 s (JsonlTailer):         ║│
  │  ║     session.started/ended/crashed/idle, usage.recorded,                ║│
  │  ║     message.user_sent / assistant_done, tool.invoked/completed,        ║│
  │  ║     rate_limit_hit, session.waiting_input / _auth (from H3 hooks)      ║│
  │  ║   file_state row per JSONL persists last (offset, mtime) every 30 s.  ║│
  │  ╚═╦═══════════╦═══════════╦═══════════╦═══════════╦═══════════╦═════════╝│
  │    │           │           │           │           │           │           │
  │    ▼           ▼           ▼           ▼           ▼           ▼           │
  │ ┌─────┐  ┌──────────┐ ┌─────────┐ ┌────────┐ ┌────────┐ ┌───────────┐  │
  │ │ M5  │  │  M3      │ │ Auto    │ │ Budget │ │Superviso│ │ Tool     │  │
  │ │Token│  │ Notif    │ │ Runner  │ │ Eval   │ │r Agent  │ │Invocation│  │
  │ │     │  │  Bus     │ │         │ │        │ │ (LLM)   │ │ Logger   │  │
  │ │Agg. │  │routing / │ │finalize │ │60 s    │ │post-run │ │writes    │  │
  │ │Agent│─►│dedup /   │ │ a stage │ │tick    │ │review   │ │tool_     │  │
  │ │Alert│  │In-App WS │ │ run on  │ │+ alerts│ │on end   │ │invocation│  │
  │ │Eval │  │+ Lark    │ │terminal │ │via Bus │ │         │ │per turn  │  │
  │ │(bot)│  │(--as bot)│ │ events  │ │        │ │         │ │          │  │
  │ └─────┘  └──────────┘ └─────────┘ └────────┘ └────────┘ └───────────┘  │
  │                                                                            │
  │ Background workers wired in lifespan (see order below):                    │
  │   RollupWorker (hourly rollup + raw-event TTL)                             │
  │   UsageScheduler / UsagePoller (spawns claude -p /usage every 30 min)     │
  │                                                                            │
  │ Agent module (private state, hidden from Sessions list by default):        │
  │   AgentStore + AgentConversationManager                                    │
  │   session.type ∈ {chat_agent}  (onboarding/supervisor deferred to v2)      │
  │                                                                            │
  │  ┌────────────────────────────────────────────────────────────────────┐ │
  │  │                    Shared SQLite (35 tables)                       │ │
  │  │  session · notification · agent_alert_rule · budget · project      │ │
  │  │  raw_token_event (+ jsonl_offset partial UNIQUE) · hourly_rollup   │ │
  │  │  hit_observation · file_state · pricing_config · usage_snapshot    │ │
  │  │  workflow_definition · mission · stage_execution (was run) · output│ │
  │  │  schedule_entry · agent_definition · agent_conversation · skill    │ │
  │  │  sync_{config,policy,activity} · drift_record · mcp_server         │ │
  │  └────────────────────────────────────────────────────────────────────┘ │
  └────────────────────────────────────────────────────────────────────────────┘

══════════════════════════════════════════════════════════════════════════════════
                                  EXTERNAL WORLD
══════════════════════════════════════════════════════════════════════════════════
   ┌──────────────────┐    ┌─────────────────────┐    ┌────────────────────┐
   │  Claude subproc  │    │ ~/.claude/projects/ │    │  OS: ss / lsof     │
   │  (PTY, M1 owns)  │    │  *.jsonl (M2 tails) │    │  listening sockets │
   └──────────────────┘    └─────────────────────┘    └────────────────────┘
   ┌──────────────────┐    ┌─────────────────────┐
   │  Lark IM (bot)   │    │  `claude -p` shots  │
   │  via lark-cli    │    │  agent-alert /      │
   │  (--as bot)      │    │  escalation /       │
   │                  │    │  workflow authoring │
   └──────────────────┘    └─────────────────────┘
```

## Core data flows

| # | Direction | Description |
|---|---|---|
| ① | UI → M1 → Claude child | POST `/api/sessions` → SessionManager forks PTY → claude spawns with `--session-id` (or `--resume`) + hooks-injected `--settings` |
| ② | Claude → JSONL → M2 → subscribers | claude writes JSONL → EventStream tails every 5 s → fan-out to Token/Notification/Automation/Supervisor/ToolLogger |
| ③ | Scheduler → Orchestrator → M1 → Claude | Cron tick → `orchestrator.launch_mission()` → stage-by-stage spawns AUTO sessions via M1 |
| ④ | NotificationBus → InAppSink → UI | Event → routing table → dedup window → WebSocket push to `/api/notifications/ws` |
| ⑤ | NotificationBus → LarkSink (bot) → Lark | For rules with `channels: ["lark"]` and known chat/user id, formatted as `【PowerGrandFather】- <title>\n<body>` shelled through `lark-cli --as bot` |
| ⑥ | AgentAlertEvaluator → sandboxed check_script → Bus/escalation | Per-rule tick → base64(window) + stdin script in isolated `python -c` subprocess; if `fired=True` and `escalate=True`, spawns `claude -p` for a root-cause title/body |
| ⑦ | Workflow authoring (UI) → generator → claude -p → YAML on disk | POST `/api/workflows/generate` spawns `claude -p` in the target repo, waits for `wrote <TASKS_DIR>/<name>.workflow.yaml`, then runs R9-R19 reviewer + upserts `workflow_definition` |

## Cross-cutting conventions

1. **EventStream is a bus, not storage.** Consumers own persistence for whatever they need to replay.
2. **SQLite is the only datastore.** No Redis / Postgres / broker — ADR-0002.
3. **All external world goes through adapters** — `csm.adapters.{claude_subprocess, jsonl_tail, inapp_sink, lark_sink}`.
4. **PTY passthrough + JSONL sidechannel.** UI terminal is raw PTY bytes; structured events come from JSONL — never parse ANSI to decide state.
5. **`Session` is the unified abstraction.** `Session.type` ∈ {interactive, auto, chat_agent} in v1 — same table, same lifecycle hooks. (`onboarding_agent` / `supervisor_agent` were reserved in v1 but never constructed; both deferred to v2 per `docs/known_issues.md`.)
6. **No module-level singletons for subsystems.** Everything is constructed in `lifespan()` and attached to `app.state.*` (see order below). The one intentional exception is `get_sessionmaker()` from `csm.db`.
7. **AUTO sessions launched by orchestrator carry hooks + `--dangerously-skip-permissions`** so CSM is the single source of truth for state changes.

## Module dependency graph

```
M2 EventStream           (foundation — no deps)
  ├─ M3 NotificationBus              (subscribes to many event types; drives InApp + Lark)
  ├─ TokenAggregator                 (persists usage.recorded)
  ├─ ToolInvocationLogger            (persists tool.invoked/completed)
  ├─ BudgetEvaluator                 (60 s tick; emits token_warning via Bus)
  ├─ AgentAlertEvaluator             (per-rule tick; sandboxed check_script; optional escalate)
  ├─ AutomationRunner                (subscribes to session.ended/crashed + assistant_done + idle)
  ├─ WorkflowOrchestrator            (subscribes to session.ended + idle; drives mission state)
  └─ SupervisorAgent                 (subscribes to session.ended; opens post-run review chat)

M1 SessionManager        (foundation — depends on EventStream for lifecycle bind)
  ├─ WorkflowOrchestrator            (spawns AUTO sessions per stage)
  └─ AgentConversationManager        (spawns *_agent sessions for chat)

AutomationScheduler      (cron ticks → orchestrator.launch_mission)
```

### WorkflowOrchestrator internal components (split 2026-07-25)

`modules/workflow/orchestrator.py` is the public entry point (class
`WorkflowOrchestrator`); two helper modules hold the heavier logic and are
called through thin delegator methods on the class:

- `orchestrator_reaper.py` — rescuer loop, `rescue_pass`, `startup_reap`,
  `finalize_mission_succeeded` / `_failed`. Change here when adjusting mission
  rescue / timeout / finalize semantics.
- `orchestrator_state.py` — `write_state_yaml`, `build_state_snapshot`,
  `STATE_YAML_FILENAME`, `STATE_YAML_HEADER`. Change here when altering the
  per-mission STATE.yaml schema or on-disk layout.

Public API surface (`launch_mission`, event subscriptions, delegator methods
like `_rescuer_loop`) is unchanged.

## Lifespan order (startup → shutdown)

Startup — see `backend/csm/main.py:59-263`:

1. `EventStream.start()` — JSONL tail + watchdog loops.
2. `SessionManager` — construct, then `startup_reap_orphans()` for rows left RUNNING across a restart.
3. `refresh_pricing_overrides(sm)` — load per-family PricingConfig rows so subsequent `TokenAggregator` inserts use current rates.
4. `TokenAggregator.start()` — subscribes to usage.recorded / rate_limit_hit.
5. `ToolInvocationLogger.start()` — subscribes to tool.invoked/completed.
6. `BudgetEvaluator.start()` — 60 s tick loop.
7. `AgentAlertEvaluator.start()` — loads rules, wires `_escalation_callback` closure that calls `escalate()` (spawns `claude -p`).
8. `InAppSink` (in-memory) + `LarkSink(sessionmaker=sm)` (reads `lark_settings` singleton row on every `send()` — config editable via `GET/PUT /api/settings/lark`; env vars kept for one release as migration-only seed), then `NotificationBus.start()` — subscribes to all routable events.
9. `AgentStore` + `AgentConversationManager` (agent module private state).
10. `AutomationRunner.start()` — subscribes to terminal + assistant_done + idle.
11. `AutomationScheduler` constructed (started later — see step 14).
12. `WorkflowLoader.load_directory(tasks_dir)` — parses `tasks/*.workflow.yaml`; `ClarificationCache` for the two-round authoring flow.
13. `WorkflowOrchestrator.start()` — subscribes to session.ended + idle for missions.
14. `scheduler._orch = orchestrator`; `AutomationScheduler.start()` — APScheduler ticks now dispatch to `launch_mission`.
15. `SupervisorAgent.start()` — subscribes to session.ended.
16. `RollupWorker.start()` — hourly rollup + raw-event TTL (default 30 d).
17. `UsageScheduler.start()` — `/usage` probe every 30 min via `UsagePoller`.

Shutdown reverses this order (see `main.py:266-279`): `UsageScheduler → Supervisor → Orchestrator → Rollup → Scheduler → Runner → NotificationBus → AgentAlertEval → BudgetEval → ToolLogger → TokenAgg → SessionManager → EventStream`.

## Tech choices summary

| Layer | Choice | Why |
|---|---|---|
| Backend language | Python 3.11 | Existing token monitor is Python; async ecosystem good enough |
| Web framework | FastAPI | Async + WebSocket first-class |
| ORM | SQLAlchemy 2.x + aiosqlite | Async-native; single-machine deploy |
| Migrations | Alembic | Schema drift protection |
| Scheduler | APScheduler (AsyncIOScheduler) | Battle-tested cron |
| PTY | ptyprocess | Linux-friendly, no fork-of-fork surprises |
| Frontend | Vue 3 + Vite | Lightweight SPA; no need for React ecosystem here |
| Terminal | xterm.js + `@xterm/addon-fit` | Standard |
| Chart | ECharts | Trend lines out of the box |
| IM | Lark via `lark-cli --as bot` | Reuse existing local CLI; bot identity keeps notifications distinct from user chatter |

## Out-of-process boundaries

Everything below runs OUTSIDE the FastAPI monolith:

- `claude` CLI subprocesses spawned by `SessionManager` (interactive + auto) and by `AgentConversationManager` (agent sessions).
- One-shot `claude -p` invocations:
  - Workflow authoring generator (`modules/workflow/authoring/generator.py`).
  - Agent-alert escalation (`modules/token/agent_alert/escalate.py`).
  - `/usage` probe (`modules/token/usage_polling.py`).
- Vite dev server (`npm run dev`) during frontend development — proxies `/api` to the FastAPI process.
- `lark-cli` for Lark IM delivery.
- The user's own workflow subjects (e.g. training runs the AUTO sessions supervise).

## Retired modules

For historical context — do NOT reintroduce these concepts:

- **M4 TaskDefinition** (retired 2026-07-06, commit bca23b8). Automation now has only `workflow` (YAML template) and `mission` (concrete execution). `/api/tasks/*`, `TaskList.vue`, `task_loader.py`, and `ScheduleEntry.task_def_id` are all gone. `run` table renamed to `stage_execution` (Python class still aliased `Run` for compat).
- **`AlertRule` / `AlertEvaluator` / `/api/tokens/alert-rules/*`** (retired 2026-07-10, commit 918de63). Replaced by `AgentAlertRule` + `AgentAlertEvaluator` — user writes NL, agent authors the check script. Endpoints under `/api/tokens/agent-alerts/…`. See ADR-0003.

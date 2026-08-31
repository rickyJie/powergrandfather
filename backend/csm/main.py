"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shlex
from contextlib import asynccontextmanager
from typing import Any, NamedTuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware

from csm.adapters.inapp_sink import InAppSink
from csm.adapters.lark_sink import LarkSink
from csm.api.agent_alerts import router as agent_alerts_router
from csm.api.agents import router as agents_router
from csm.api.automation import router as automation_router
from csm.api.backends import router as backends_router
from csm.api.backup import router as backup_router
from csm.api.budgets import router as budgets_router
from csm.api.events import router as events_router
from csm.api.files import router as files_router
from csm.api.fs import router as fs_router
from csm.api.hooks import router as hooks_router
from csm.api.lark_settings import router as lark_settings_router
from csm.api.metrics import router as metrics_router
from csm.api.missions import router as missions_router
from csm.api.notifications import router as notifications_router
from csm.api.preferences import router as preferences_router
from csm.api.pricing import router as pricing_router
from csm.api.projects import router as projects_router
from csm.api.proxy_env import router as proxy_env_router
from csm.api.session_projects import router as session_projects_router
from csm.api.sessions import router as sessions_router
from csm.api.sync import router as sync_router
from csm.api.tokens import router as tokens_router
from csm.api.workflows import router as workflows_router
from csm.api.worktime import router as worktime_router
from csm.backends import AdapterRegistry, build_default_registry
from csm.config import settings
from csm.core.assistant_text_backfill import backfill_last_assistant_msg
from csm.core.event_stream import EventStream
from csm.core.notification_bus import NotificationBus
from csm.core.single_instance import DbInstanceLock
from csm.db import get_sessionmaker
from csm.modules.agent.agent_store import AgentStore
from csm.modules.agent.conversation import AgentConversationManager
from csm.modules.automation.runner import AutomationRunner
from csm.modules.automation.scheduler import AutomationScheduler
from csm.modules.session_manager.manager import SessionManager
from csm.modules.supervisor.agent import SupervisorAgent
from csm.modules.sync.agent import SyncAgent
from csm.modules.sync.orchestrator import SyncOrchestrator
from csm.modules.sync.scheduler import SyncTickScheduler
from csm.modules.sync.service import DriftPoller, SyncService
from csm.modules.token.agent_alert import AgentAlertEvaluator
from csm.modules.token.agent_alert.escalate import escalate as _escalate_call
from csm.modules.token.aggregator import TokenAggregator
from csm.modules.token.budget import BudgetEvaluator
from csm.modules.token.rollup import RollupWorker
from csm.modules.token.tool_logger import ToolInvocationLogger
from csm.modules.token.usage_polling import UsagePoller
from csm.modules.token.usage_scheduler import UsageScheduler
from csm.modules.workflow.authoring import ClarificationCache
from csm.modules.workflow.loader import WorkflowLoader
from csm.modules.workflow.orchestrator import WorkflowOrchestrator
from csm.modules.worktime import HeartbeatManager, WorktimeService, WorktimeTracker

# Bump the `csm.*` logger tree to INFO so the observability lines added
# for Finding-6 (EventStream watchdog tick, SESSION_IDLE dispatch, hook
# handler swallow) actually reach csm.log. Uvicorn's --log-level only
# affects its own loggers; csm application loggers default to WARNING
# and root has no handler at import time, so we attach our own explicit
# stream handler here to guarantee delivery regardless of what uvicorn
# reconfigures during its startup.
_csm_logger = logging.getLogger("csm")
if not _csm_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    _csm_logger.addHandler(_handler)
    _csm_logger.setLevel(logging.INFO)
    _csm_logger.propagate = False


class Subsystems(NamedTuple):
    """All lifespan-managed subsystems in construction order.

    Ordering is meaningful: it matches the lifespan build sequence
    (dependencies before dependents). Start/stop scheduling in `lifespan`
    references these fields explicitly; the reverse-order teardown is
    hand-written rather than derived so we can be defensive around
    per-subsystem stop semantics.
    """

    sessionmaker: async_sessionmaker
    adapter_registry: AdapterRegistry
    event_stream: EventStream
    session_manager: SessionManager
    startup_orphans_marked: int
    token_aggregator: TokenAggregator
    tool_logger: ToolInvocationLogger
    budget_evaluator: BudgetEvaluator
    agent_alert_evaluator: AgentAlertEvaluator
    inapp_sink: InAppSink
    lark_sink: LarkSink
    notification_bus: NotificationBus
    agent_store: AgentStore
    agent_conv_manager: AgentConversationManager
    tasks_loaded_count: int
    automation_runner: AutomationRunner
    automation_scheduler: AutomationScheduler
    workflow_loader: WorkflowLoader
    workflow_clarify_cache: ClarificationCache
    workflows_loaded_count: int
    orchestrator: WorkflowOrchestrator
    supervisor_agent: SupervisorAgent
    rollup_worker: RollupWorker
    # `usage_poller` is intentionally absent: UsageScheduler owns it and
    # nothing else reads it, so carrying it here only created a field that
    # never reached app.state.
    usage_scheduler: UsageScheduler
    sync_service: SyncService
    drift_poller: DriftPoller
    # sync v2 agent-driven (Phase 5 lifespan integration)
    sync_agent: SyncAgent
    sync_orchestrator: SyncOrchestrator
    sync_scheduler: SyncTickScheduler
    # worktime (header top-right widget)
    worktime_service: WorktimeService
    worktime_tracker: WorktimeTracker
    worktime_heartbeat: HeartbeatManager
    worktime_reaped: int


async def _probe_and_merge_sync_capabilities(registry: AdapterRegistry) -> None:
    """Union each adapter's probed SYNC_* capabilities into its static set.

    Static caps only declare SYNC_MEMORY; SYNC_MCP / SYNC_SKILLS depend on the
    installed CLI version and surface ONLY via `probe_sync_capabilities`
    (base.py contract). The probe result used to be dead — nothing wrote it
    back — so SyncService saw `{sync_memory}` and every mcp/skill push
    silently returned UNSUPPORTED for all agents. Integration tests masked it
    by injecting caps onto mock adapters.

    UNION, never replace: non-sync capabilities (hooks, interactive_stream,
    post_spawn_bind, …) must survive. Probes run in parallel and a failure
    degrades to static caps rather than blocking boot.
    """
    async def _apply(adapter: object) -> None:
        try:
            probed = await adapter.probe_sync_capabilities()
        except Exception:  # noqa: BLE001 — never block boot on a CLI probe
            logging.getLogger(__name__).warning(
                "probe_sync_capabilities failed for %s; keeping static caps",
                getattr(adapter, "name", "?"),
                exc_info=True,
            )
            return
        adapter.capabilities = adapter.capabilities | probed

    await asyncio.gather(*(_apply(a) for a in registry.all()))


def _hooks_callback_base_url() -> str:
    """Base URL the spawned CLI posts hook events back to.

    Always loopback, decoupled from the public bind: with `host=0.0.0.0` the
    hook URL would otherwise be a wildcard address some HTTP clients refuse to
    dial.

    The scheme must track whether uvicorn ACTUALLY boots TLS, not merely
    whether cert files exist on disk — start.sh only enables TLS under
    CSM_ENABLE_TLS=1. Getting this wrong breaks it in both directions:
    http:// against an HTTPS server gives "Empty reply from server", https://
    against a plaintext one gives "Invalid HTTP request received". Hence the
    flag AND the cert files.
    """
    tls_on = settings.enable_tls and settings.resolved_ssl_paths() is not None
    scheme = "https" if tls_on else "http"
    return f"{scheme}://127.0.0.1:{settings.port}"


async def _build_sync_stack(
    sm: async_sessionmaker,
    adapter_registry: AdapterRegistry,
    session_manager: SessionManager,
    event_stream: EventStream,
) -> dict[str, Any]:
    """Multi-agent config sync — v1 (retired) plus v2 agent-driven.

    Returns the `Subsystems` fields it owns, so the caller splices with `**`
    and a new field can't be built here and silently forgotten there
    (`test_lifespan.py::test_every_subsystem_field_reaches_app_state`).
    """
    # SyncService is pure logic (no background task).
    sync_service = SyncService(sessionmaker=sm, adapter_registry=adapter_registry)
    # v1 `lock` mode is retired — sync is agent-driven only. DriftPoller is
    # still constructed for compat (Subsystems tuple + teardown + health map)
    # but deliberately NOT started, so no rule-driven drift ever runs. Delete
    # the class + /drift endpoints in a follow-up cleanup.
    drift_poller = DriftPoller(
        sessionmaker=sm,
        adapter_registry=adapter_registry,
        sync_service=sync_service,
        tick_interval_sec=settings.sync_drift_tick_sec,
    )

    # v2 opt-in per module via sync_config.sync_mode. Both versions write the
    # same resource tables; v2 additionally writes fanout_ledger +
    # sync_agent_run. The primary decide path spawns an AUTO session under the
    # user's default agent (so no ANTHROPIC_API_KEY is needed) — hence the
    # session manager + event stream. It falls back to a direct Anthropic call
    # only when those are absent, which is the bare-test case.
    sync_agent_v2 = SyncAgent(
        sessionmaker=sm,
        session_manager=session_manager,
        event_stream=event_stream,
    )
    sync_orchestrator = SyncOrchestrator(
        sessionmaker=sm,
        adapter_registry=adapter_registry,
        sync_service=sync_service,
        sync_agent=sync_agent_v2,
    )
    await _replay_pending_fanout_ledger(sync_orchestrator)

    sync_scheduler = SyncTickScheduler(orchestrator=sync_orchestrator, sessionmaker=sm)
    await sync_scheduler.start()
    return {
        "sync_service": sync_service,
        "drift_poller": drift_poller,
        "sync_agent": sync_agent_v2,
        "sync_orchestrator": sync_orchestrator,
        "sync_scheduler": sync_scheduler,
    }


async def _replay_pending_fanout_ledger(orchestrator: SyncOrchestrator) -> None:
    """Re-drive phase2_done ledger rows left over from a previous process.

    Bounded at 30s (v7 §2) so a large backlog can't hold up boot; whatever is
    still pending is picked up by the next scheduled tick. Never raises —
    a failed replay must degrade to "try again next tick", not to "no server".
    """
    if not orchestrator.try_acquire_tick():
        return
    try:
        await asyncio.wait_for(orchestrator.replay_pending_fanout_ledger(), timeout=30.0)
    except TimeoutError:
        logging.getLogger(__name__).warning(
            "sync v2 startup replay exceeded 30s; remaining entries "
            "will be handled by next scheduled tick",
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "sync v2 startup replay raised; continuing boot",
        )
    finally:
        orchestrator.release_tick()


async def _build_worktime_stack(
    sm: async_sessionmaker, event_stream: EventStream,
) -> dict[str, Any]:
    """Worktime widget (header, top-right).

    Service handles reads + boot reap; tracker subscribes to EventStream for
    MESSAGE_USER_SENT / DONE; heartbeat receives frontend pings.
    """
    worktime_service = WorktimeService(sessionmaker=sm)
    worktime_reaped = await worktime_service.reap_orphans_on_boot()
    worktime_tracker = WorktimeTracker(sessionmaker=sm, event_stream=event_stream)
    await worktime_tracker.start()
    worktime_heartbeat = HeartbeatManager(sessionmaker=sm)
    await worktime_heartbeat.start()
    return {
        "worktime_service": worktime_service,
        "worktime_tracker": worktime_tracker,
        "worktime_heartbeat": worktime_heartbeat,
        "worktime_reaped": worktime_reaped,
    }


async def _build_subsystems(app: FastAPI) -> Subsystems:
    """Construct every lifespan-managed subsystem in dependency order.

    Some subsystems need to be `start()`ed during construction so that
    later ones observe the correct initial state (e.g. TokenAggregator
    must load pricing overrides *before* subscribing to the event stream
    so the first USAGE_RECORDED sees correct rates). Keep the order in
    lockstep with the pre-refactor lifespan body.
    """
    sm = get_sessionmaker()

    # Multi-agent v2: adapter registry is the source of truth for which
    # CLI backends are available and how to spawn / tail them. Built
    # here so downstream subsystems (EventStream, SessionManager) share
    # the same instance.
    adapter_registry = build_default_registry()

    await _probe_and_merge_sync_capabilities(adapter_registry)

    event_stream = EventStream(
        sessionmaker=sm,
        projects_root=settings.claude_projects_dir,
        poll_interval_sec=settings.event_stream_poll_interval_sec,
        watchdog_interval_sec=settings.event_stream_watchdog_interval_sec,
        session_idle_minutes=settings.session_idle_minutes,
        adapter_registry=adapter_registry,
    )
    await event_stream.start()

    _argv_override = shlex.split(settings.claude_argv) if settings.claude_argv else None
    _hooks_base = _hooks_callback_base_url()
    # One-shot sniff of the user's login shell + optional
    # ~/.csm/proxy.env; the result is layered into every spawned session's
    # env. See csm.modules.session_manager.env for the policy.
    from csm.modules.session_manager.env import resolve_proxy_env

    _proxy_resolve = resolve_proxy_env(
        auto_sniff=settings.proxy_auto_sniff,
        env_file=settings.proxy_env_file,
        sniff_timeout=settings.proxy_sniff_timeout_sec,
    )
    for _w in _proxy_resolve.warnings:
        logging.getLogger(__name__).info("proxy env: %s", _w)
    session_manager = SessionManager(
        sessionmaker=sm,
        event_stream=event_stream,
        ring_buffer_bytes=settings.ring_buffer_bytes,
        stop_grace_sec=settings.session_stop_grace_sec,
        claude_argv=_argv_override,
        hooks_base_url=_hooks_base,
        adapter_registry=adapter_registry,
        proxy_env=_proxy_resolve.env,
    )
    # Stash the resolve metadata on app.state so /api/settings/proxy-env
    # can render provenance (sniffed vs file, warnings) without re-running
    # the sniff on every request.
    app.state.proxy_resolve = _proxy_resolve
    reaped = await session_manager.startup_reap_orphans()
    await session_manager.start()

    # Construct aggregator first so it can own the pricing overrides cache,
    # then load any per-family PricingConfig rows BEFORE `start()` subscribes
    # to the event stream — this way the first ingested USAGE_RECORDED event
    # already sees the correct override rates.
    token_agg = TokenAggregator(sessionmaker=sm, event_stream=event_stream)
    await token_agg.refresh_pricing_overrides(sm)
    await token_agg.start()

    tool_logger = ToolInvocationLogger(sessionmaker=sm, event_stream=event_stream)
    await tool_logger.start()

    budget_eval = BudgetEvaluator(sessionmaker=sm, event_stream=event_stream, interval_sec=60.0)
    await budget_eval.start()

    async def _escalation_callback(rule, window, script_payload):
        result = await _escalate_call(
            sessionmaker=sm,
            rule=rule,
            window_snapshot=window,
            script_payload=script_payload,
        )
        if result is None:
            return None
        return {
            "title": result.title,
            "body": result.body,
            "duration_sec": round(result.duration_sec, 2),
        }

    agent_alert_eval = AgentAlertEvaluator(
        sessionmaker=sm,
        event_stream=event_stream,
        escalation_callback=_escalation_callback,
    )
    await agent_alert_eval.start()

    inapp_sink = InAppSink()
    # LarkSink reads the singleton `lark_settings` row on every send();
    # UI edits via PUT /api/settings/lark take effect without restart.
    # `send()` self-skips when the row is missing / disabled / has no
    # target, so passing the sink unconditionally is safe.
    lark_sink = LarkSink(sessionmaker=sm)
    notif_bus = NotificationBus(
        sessionmaker=sm,
        event_stream=event_stream,
        in_app_sink=inapp_sink,
        dedup_window_sec=settings.notif_dedup_window_sec,
        # The hook Stop event fires instantly; the JSONL tail reports the same
        # turn up to one poll later. Anything inside poll+slack is one turn.
        cross_source_window_sec=settings.event_stream_poll_interval_sec + 3.0,
        # Always pass the sink: it self-skips when no default target and no
        # per-event lark_chat_id / lark_user_id is supplied in metadata.
        lark_sink=lark_sink,
    )
    await notif_bus.start()

    # ---- agents ----
    agent_store = AgentStore(sessionmaker=sm)
    agent_conv_manager = AgentConversationManager(
        sessionmaker=sm,
        session_manager=session_manager,
    )

    # ---- automation ----
    tasks_dir = settings.tasks_dir
    if not tasks_dir.is_absolute():
        tasks_dir = settings.project_root / tasks_dir
    # M4 TaskLoader is retired (P2). The `task_loader` app state attribute
    # is left unset; any legacy caller reading it should switch to
    # `workflow_loader` further below.
    tasks_loaded_count = 0

    runner = AutomationRunner(
        sessionmaker=sm,
        event_stream=event_stream,
        session_manager=session_manager,
        grace_sec=settings.auto_grace_sec,
        notification_bus=notif_bus,
    )
    await runner.start()

    # Scheduler is created here but NOT started yet — its `_fire_run` needs to
    # dispatch to the orchestrator for M8 workflow schedules, so we build the
    # orchestrator first and then start the scheduler.
    scheduler = AutomationScheduler(sessionmaker=sm, runner=runner)

    # ---- M8: workflow definitions (*.workflow.yaml) ----
    workflow_loader = WorkflowLoader(sessionmaker=sm)

    # In-memory TTL cache backing the two-round workflow authoring flow
    # (POST /api/workflows/clarify → POST /api/workflows/generate). Attached
    # here per project convention (no module-level singletons for subsystems).
    workflow_clarify_cache = ClarificationCache()
    try:
        wf_loaded = await workflow_loader.load_directory(tasks_dir)
        workflows_loaded_count = len(wf_loaded)
    except Exception:
        workflows_loaded_count = 0

    # ---- M8 P4: Workflow Mission orchestrator (skeleton; state machine in T2+) ----
    orchestrator = WorkflowOrchestrator(
        sessionmaker=sm,
        event_stream=event_stream,
        session_manager=session_manager,
        runner=runner,
        workflow_loader=workflow_loader,
    )
    await orchestrator.start()

    # Wire orchestrator into scheduler before starting APScheduler so cron
    # ticks for workflow schedules can dispatch to `launch_mission`.
    scheduler._orch = orchestrator
    await scheduler.start()

    # ---- F2: Supervisor agent (LLM-driven post-run review) ----
    supervisor = SupervisorAgent(sessionmaker=sm, event_stream=event_stream)
    await supervisor.start()

    # ---- F7: hourly rollup + raw event TTL ----
    # `retention_days` here is only the FALLBACK: the RollupWorker reads the live
    # window from user_preference.raw_event_retention_days each tick, so it's
    # retunable via PUT /api/preferences without a restart. The settings value
    # seeds that behaviour when the preference row can't be read.
    rollup_worker = RollupWorker(
        sessionmaker=sm,
        tick_interval_sec=settings.rollup_tick_interval_sec,
        retention_days=settings.raw_event_retention_days,
    )
    await rollup_worker.start()

    # ---- /usage live probe: half-hourly background refresh ----
    usage_poller = UsagePoller(probe_cwd=str(settings.usage_probe_cwd))
    usage_scheduler = UsageScheduler(
        poller=usage_poller,
        sessionmaker=sm,
        interval_min=settings.usage_poll_interval_min,
        adapter_registry=adapter_registry,
    )
    usage_scheduler.start()

    sync_stack = await _build_sync_stack(
        sm, adapter_registry, session_manager, event_stream,
    )
    worktime_stack = await _build_worktime_stack(sm, event_stream)

    return Subsystems(
        sessionmaker=sm,
        adapter_registry=adapter_registry,
        event_stream=event_stream,
        session_manager=session_manager,
        startup_orphans_marked=reaped,
        token_aggregator=token_agg,
        tool_logger=tool_logger,
        budget_evaluator=budget_eval,
        agent_alert_evaluator=agent_alert_eval,
        inapp_sink=inapp_sink,
        lark_sink=lark_sink,
        notification_bus=notif_bus,
        agent_store=agent_store,
        agent_conv_manager=agent_conv_manager,
        tasks_loaded_count=tasks_loaded_count,
        automation_runner=runner,
        automation_scheduler=scheduler,
        workflow_loader=workflow_loader,
        workflow_clarify_cache=workflow_clarify_cache,
        workflows_loaded_count=workflows_loaded_count,
        orchestrator=orchestrator,
        supervisor_agent=supervisor,
        rollup_worker=rollup_worker,
        usage_scheduler=usage_scheduler,
        **sync_stack,
        **worktime_stack,
    )


def _wire_state(app: FastAPI, subs: Subsystems) -> None:
    """Publish subsystem handles on `app.state` for router-side lookup."""
    # sessionmaker is exposed so API routes can pick it up via
    # `Depends(get_db_sessionmaker)` (see csm.api._deps) instead of calling
    # the module-level singleton directly — makes it swappable for tests
    # without global monkey-patching.
    app.state.sessionmaker = subs.sessionmaker
    app.state.adapter_registry = subs.adapter_registry
    app.state.event_stream = subs.event_stream
    app.state.session_manager = subs.session_manager
    app.state.startup_orphans_marked = subs.startup_orphans_marked
    app.state.token_aggregator = subs.token_aggregator
    app.state.tool_logger = subs.tool_logger
    app.state.budget_evaluator = subs.budget_evaluator
    app.state.agent_alert_evaluator = subs.agent_alert_evaluator
    app.state.inapp_sink = subs.inapp_sink
    app.state.lark_sink = subs.lark_sink
    app.state.notification_bus = subs.notification_bus
    app.state.agent_store = subs.agent_store
    app.state.agent_conv_manager = subs.agent_conv_manager
    app.state.tasks_loaded_count = subs.tasks_loaded_count
    app.state.automation_runner = subs.automation_runner
    app.state.automation_scheduler = subs.automation_scheduler
    app.state.workflow_loader = subs.workflow_loader
    app.state.workflow_clarify_cache = subs.workflow_clarify_cache
    app.state.workflows_loaded_count = subs.workflows_loaded_count
    app.state.orchestrator = subs.orchestrator
    app.state.supervisor_agent = subs.supervisor_agent
    app.state.rollup_worker = subs.rollup_worker
    app.state.usage_scheduler = subs.usage_scheduler
    app.state.sync_service = subs.sync_service
    app.state.drift_poller = subs.drift_poller
    # sync v2 agent-driven
    app.state.sync_agent = subs.sync_agent
    app.state.sync_orchestrator = subs.sync_orchestrator
    app.state.sync_scheduler = subs.sync_scheduler
    # worktime
    app.state.worktime_service = subs.worktime_service
    app.state.worktime_tracker = subs.worktime_tracker
    app.state.worktime_heartbeat = subs.worktime_heartbeat
    app.state.worktime_reaped = subs.worktime_reaped


def _require_sandbox_on_codex_branch(adapter_registry: AdapterRegistry) -> None:
    """Refuse to boot on the multi-CLI integration branch without sandbox env.

    Presence of `.codex-dev-branch` marker at project root indicates this
    checkout is the multi-CLI integration branch. Every registered
    adapter's `home_dir()` MUST NOT resolve to the real
    `~/.<default_home_name>` — otherwise the running server could touch
    real user config.

    Iterates the registry rather than hardcoding {claude, codex} so that
    adding a third adapter (e.g. gemini) auto-inherits the guard.
    """
    from pathlib import Path as _Path

    marker = settings.project_root / ".codex-dev-branch"
    if not marker.exists():
        return  # main / other branches — no restriction

    errors: list[str] = []
    for adapter in adapter_registry.all():
        try:
            home_resolved = adapter.home_dir().resolve()
        except (OSError, RuntimeError):
            continue
        real_home = (_Path.home() / adapter.default_home_name()).resolve()
        if home_resolved == real_home:
            errors.append(
                f"CSM_{adapter.name.upper()}_HOME not set — "
                f"{adapter.name}.home_dir() resolves to real {home_resolved}."
            )
    if errors:
        msg = (
            "\n\n=== CSM CODEX-BRANCH SANDBOX GUARD ===\n"
            + "\n".join(f"  - {e}" for e in errors)
            + "\n\nThis branch (llj_dev_codex) MUST NOT touch your real user config.\n"
            + "Boot refused. Source the sandbox before starting:\n"
            + "    source scripts/csm-codex-dev-sandbox.sh\n"
            + "    ./scripts/dev.sh    # or ./scripts/start.sh\n"
        )
        raise RuntimeError(msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    # Single-instance guard (ADR-0002): refuse to boot a second backend against
    # the same SQLite file. Two owners of one csm.db race on NEW_MESSAGE /
    # unread / external_session_id rebinds and silently break tab sync — see
    # csm.core.single_instance. Acquire BEFORE opening the DB / building
    # subsystems. Opt out with CSM_SKIP_DB_LOCK=1 (test harnesses that reuse the
    # default path in-process).
    app.state.db_lock = None
    if os.environ.get("CSM_SKIP_DB_LOCK") != "1":
        _db_lock = DbInstanceLock(settings.resolved_db_path())
        _db_lock.acquire()  # raises SingleInstanceError → uvicorn aborts startup
        app.state.db_lock = _db_lock

    # Build registry FIRST so the sandbox guard can iterate every adapter's
    # home_dir(). Then re-use the same instance in _build_subsystems.
    _sandbox_registry = build_default_registry()
    _require_sandbox_on_codex_branch(_sandbox_registry)
    subs = await _build_subsystems(app)
    _wire_state(app, subs)

    # ---- Backfill last_assistant_msg for pre-fix sessions (5de334d5) ----
    # EventStream now supplies assistant text through MESSAGE_ASSISTANT_DONE,
    # but sessions started before the fix have NULL last_assistant_msg with
    # no upcoming event to populate them (already ended or long-idle). This
    # one-shot startup task scans them and reads the tail of their JSONL to
    # fill the gap. Fire-and-forget: a slow disk shouldn't block startup.
    app.state.backfill_task = asyncio.create_task(
        backfill_last_assistant_msg(subs.sessionmaker, settings.claude_projects_dir)
    )

    yield

    # ---- shutdown (reverse of startup order) ----
    bt = getattr(app.state, "backfill_task", None)
    if bt and not bt.done():
        bt.cancel()
        try:
            await asyncio.wait_for(bt, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            logging.getLogger(__name__).exception("backfill_task raised on shutdown")
    await subs.worktime_heartbeat.stop()
    await subs.worktime_tracker.stop()
    await subs.sync_scheduler.stop()
    await subs.drift_poller.stop()
    await subs.usage_scheduler.stop()
    await subs.supervisor_agent.stop()
    await subs.orchestrator.stop()
    await subs.rollup_worker.stop()
    await subs.automation_scheduler.stop()
    await subs.automation_runner.stop()
    await subs.notification_bus.stop()
    await subs.agent_alert_evaluator.stop()
    await subs.budget_evaluator.stop()
    await subs.tool_logger.stop()
    await subs.token_aggregator.stop()
    await subs.session_manager.shutdown()
    await subs.event_stream.stop()
    # Release the single-instance DB lock last (it was acquired first). flock
    # also drops automatically on process exit, so this is just tidy handover
    # for graceful restarts / --reload.
    if getattr(app.state, "db_lock", None) is not None:
        app.state.db_lock.release()


app = FastAPI(
    title="PowerGrandFather - Claude Session Manager",
    version="0.1.0",
    lifespan=lifespan,
)


class RequireClientHeaderMiddleware(BaseHTTPMiddleware):
    """C5 — require X-CSM-Client header on /api/* to force preflight
    (defense-in-depth against CSRF via form-encoded POST from a browser
    on a stray tab). Skip: /api/hooks/* (already loopback+Host gated),
    /api/metrics (Prometheus scraper), /api/events/stream (EventSource
    can't set custom headers), and the file-preview GET endpoints —
    those are opened via `window.open` / `<img src>` / `<a href
    download>` in the frontend, which browsers don't let carry custom
    headers. All three are GET-only and side-effect-free (preview =
    read-only render, raw = FileResponse, oss-redirect = 302 to a
    whitelisted key), so CSRF exposure is nil. WS routes bypass
    automatically because BaseHTTPMiddleware only sees the `http` ASGI
    scope.
    """

    _EXEMPT_PREFIXES = (
        "/api/hooks/",
        "/api/metrics",
        "/api/events/stream",
        "/api/files/preview",
        "/api/files/raw",
        "/api/files/inline/",  # HTML preview iframe + its sibling <video>/<img>/<link>/<script>
        "/api/files/oss-redirect",
    )
    # Suffix match: session-scoped GET endpoints opened via `window.open`
    # (browsers can't attach custom headers on new-tab navigation).
    # `/api/sessions/<sid>/changes/diff-view` — the diff view is a
    # server-side rendered HTML page like preview, GET-only, side-effect-free.
    _EXEMPT_SUFFIXES = ("/changes/diff-view",)

    async def dispatch(self, request, call_next):
        path = request.url.path
        token_bootstrap = False
        if settings.access_token and not path.startswith("/api/hooks/"):
            query_token = request.query_params.get("token")
            supplied = (
                request.cookies.get("csm_access_token")
                or request.headers.get("x-csm-token")
                or query_token
            )
            if not supplied or not secrets.compare_digest(supplied, settings.access_token):
                return JSONResponse(
                    {"detail": "invalid or missing CSM access token"},
                    status_code=401,
                )
            token_bootstrap = query_token is not None
        if (
            path.startswith("/api/")
            and not any(path.startswith(p) for p in self._EXEMPT_PREFIXES)
            and not any(path.endswith(s) for s in self._EXEMPT_SUFFIXES)
        ):
            # OPTIONS preflight passes through (CORS middleware handles it).
            if request.method != "OPTIONS":
                if request.headers.get("x-csm-client") != "1":
                    return JSONResponse(
                        {"detail": "missing X-CSM-Client: 1 header (see /docs)"},
                        status_code=400,
                    )
        response = await call_next(request)
        if token_bootstrap:
            # Set before the browser parses index.html, so module/CSS requests
            # are authenticated even though frontend JavaScript has not run.
            response.set_cookie(
                "csm_access_token",
                settings.access_token,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="strict",
                path="/",
            )
        return response


# Order matters: middlewares run outer→inner in the order they're added
# in reverse (last-added runs first). We want CORS to run FIRST so
# preflight (OPTIONS) requests are handled before header enforcement,
# hence add the CORS middleware LAST.
# Compress JSON before it goes down the SSH tunnel. Nothing here was
# compressed: a session list is 227KB on the wire and the desktop's history
# page 180KB, all of it re-sent on every cold start of a phone that reaches
# this server through a port-forward. Measured on real payloads, gzip -6 cuts
# both by ~62% (227KB→85KB, 180KB→69KB); they don't hit 10x because the bulk
# is UUIDs and absolute paths.
#
# Registered FIRST so it ends up INNERMOST (add_middleware prepends; the last
# one added runs outermost). That placement is load-bearing: the outer
# `RequireClientHeaderMiddleware` is a BaseHTTPMiddleware, which re-emits the
# response as a stream and drops Content-Length — and GZipMiddleware falls
# back to compressing unconditionally when it can't see a length. Sitting
# outside it, `minimum_size` was silently dead and every 60-byte /api/health
# poll got gzipped (verified: it came back `content-encoding: gzip`).
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(RequireClientHeaderMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-CSM-Client"],
)

# Opt-in request-latency logging (CSM_PERF_LOG=1). Added LAST so it is the
# outermost middleware and times the full server handling; correlates with the
# frontend via X-Request-Id (see csm.core.perf_log). Zero cost when disabled.
from csm.core.perf_log import PerfLogMiddleware, perf_enabled  # noqa: E402
from csm.core.perf_log import router as perf_router  # noqa: E402

if perf_enabled():
    app.add_middleware(PerfLogMiddleware)
    logging.getLogger("csm").info("perf logging ENABLED → perf.log (CSM_PERF_LOG=1)")

# Mount the client-perf ingest route UNCONDITIONALLY. The frontend beacon POSTs
# to /api/clientperf on slow/failed requests regardless of the server's
# CSM_PERF_LOG setting; if the route isn't registered the POST falls through to
# the SPA catch-all (`/{spa_path:path}`, GET-only) and returns 405 Method Not
# Allowed — 700+ such 405s were spamming the log. The handler is a cheap no-op
# when perf logging is disabled (drains the body, writes nothing), so it's safe
# to always expose.
app.include_router(perf_router)

app.include_router(sessions_router)
app.include_router(tokens_router)
app.include_router(agent_alerts_router)
app.include_router(budgets_router)
app.include_router(pricing_router)
app.include_router(metrics_router)
app.include_router(notifications_router)
app.include_router(automation_router)
app.include_router(hooks_router)
app.include_router(backup_router)
app.include_router(fs_router)
app.include_router(agents_router)
app.include_router(workflows_router)
app.include_router(projects_router)
app.include_router(session_projects_router)
app.include_router(missions_router)
app.include_router(files_router)
app.include_router(events_router)
app.include_router(backends_router)
app.include_router(preferences_router)
app.include_router(sync_router)
app.include_router(proxy_env_router)
app.include_router(lark_settings_router)
app.include_router(worktime_router)


@app.get("/api/health")
async def health(request: Request) -> dict[str, object]:
    """Report background-loop liveness, not merely HTTP responsiveness."""
    task_specs = {
        "event_tail": ("event_stream", "_tail_task"),
        "event_watchdog": ("event_stream", "_watchdog_task"),
        "notification_retention": ("notification_bus", "_retention_task"),
        "workflow_rescuer": ("orchestrator", "_rescuer_task"),
        "sync_drift": ("drift_poller", "_task"),
        "usage_poll": ("usage_scheduler", "_task"),
        "rollup": ("rollup_worker", "_task"),
        "ports": ("port_registry", "_task"),
    }
    tasks: dict[str, str] = {}
    for name, (state_name, attr_name) in task_specs.items():
        owner = getattr(request.app.state, state_name, None)
        task = getattr(owner, attr_name, None) if owner is not None else None
        if task is None:
            tasks[name] = "missing"
        elif task.cancelled():
            tasks[name] = "cancelled"
        elif task.done():
            tasks[name] = "failed" if task.exception() is not None else "stopped"
        else:
            tasks[name] = "running"
    degraded = [name for name, status in tasks.items() if status != "running"]
    return {
        "status": "degraded" if degraded else "ok",
        "project": "PowerGrandFather",
        "tasks": tasks,
        "degraded": degraded,
    }


@app.get("/api/version")
async def version() -> dict[str, Any]:
    return {
        "version": app.version,
        # Server-side feature gates the SPA needs at boot. `oss_base_url` is
        # empty on every install that has no OSS host — and with it empty
        # `/api/files/oss-redirect` answers 503, so the frontend must not
        # render `s3://` URIs as links. Ship the bool, not the URL: the host
        # itself is none of the browser's business.
        "oss_configured": bool(settings.oss_base_url),
    }


@app.get("/api/events/recent")
async def recent_events(limit: int = 50) -> dict:
    from csm.api._serialize import iso_utc

    stream = app.state.event_stream
    items = stream.replay()[-limit:]
    return {
        "count": len(items),
        "events": [
            {
                "id": e.id,
                "type": e.type.value,
                "ts": iso_utc(e.ts),
                "session_id": e.session_id,
                "project_path": e.project_path,
                "payload": e.payload,
            }
            for e in items
        ],
    }


_ = settings


# ---- frontend static serving (production) ----
# In dev (vite on :5173) this mount is unused; the dev server proxies /api to us.
# In prod, uvicorn alone serves both API and built SPA from frontend/dist.
_FRONTEND_DIST = settings.project_root / "frontend" / "dist"
if _FRONTEND_DIST.is_dir() and (_FRONTEND_DIST / "index.html").exists():
    app.mount(
        "/assets",
        StaticFiles(directory=_FRONTEND_DIST / "assets"),
        name="frontend-assets",
    )

    # Unhashed entry documents must revalidate. FileResponse sets only
    # last-modified + etag, and a response with no Cache-Control gets
    # HEURISTIC freshness (~10% of its age) — so an index.html that was
    # already days old when cached stays "fresh" for hours, and the client
    # never learns the new hashed chunk names after a rebuild. Assets under
    # /assets/ are content-addressed and stay cacheable.
    _ENTRY_FILES = frozenset({"index.html", "sw.js", "manifest.webmanifest"})

    def _serve_spa_file(path):
        if path.name in _ENTRY_FILES:
            return FileResponse(
                path, headers={"Cache-Control": "no-cache, must-revalidate"}
            )
        return FileResponse(path)

    @app.get("/", include_in_schema=False)
    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa_fallback(spa_path: str = ""):
        # Reserve /api and /proxy for real routers above; everything else either
        # maps to a static file at dist/ root (favicon.png, robots.txt, ...) or
        # falls through to index.html so the SPA router can take over.
        if spa_path.startswith("api/") or spa_path.startswith("proxy/"):
            raise HTTPException(status_code=404)
        if spa_path:
            candidate = (_FRONTEND_DIST / spa_path).resolve()
            dist_root = _FRONTEND_DIST.resolve()
            if candidate.is_file() and candidate.is_relative_to(dist_root):
                return _serve_spa_file(candidate)
        return _serve_spa_file(_FRONTEND_DIST / "index.html")


# ---- mobile /m/ SPA (single-process co-serving) ----
# When the mobile SPA has been built (mobile/frontend/dist), auto-mount it under
# /m/ on THIS process so one uvicorn + one csm.db serves both the desktop console
# (/) and the phone companion (/m/). This is what makes desktop↔mobile stay in
# sync (same EventStream, same JSONL) and avoids a second DB owner racing the
# single-instance lock. register() is idempotent, moves its /m/ routes ahead of
# the desktop catch-all above, and no-ops when the mobile dist is absent. Guarded
# so a desktop-only checkout (no mobile/ on sys.path) still boots.
try:
    from mobile.backend_patch.mount import register as _register_mobile

    _register_mobile(app)
except ImportError:
    # Mobile package genuinely absent (desktop-only checkout) — expected, quiet.
    pass
except Exception:  # pragma: no cover - real mount bug, not "mobile absent"
    # Don't let a mount failure kill desktop boot, but DO surface it — a silent
    # `pass` here made a broken register() look identical to "mobile not built".
    logging.getLogger(__name__).warning(
        "mobile /m/ mount failed to register", exc_info=True
    )

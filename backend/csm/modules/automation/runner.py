"""Run launcher and output discovery.

Cooperates with Session Manager: spawns an AUTO session for the task, listens
for `message.assistant_done` to end the session after a short grace, and on
`session.ended`/`session.crashed` updates the Run row + scans for outputs.
"""

from __future__ import annotations

import asyncio
import logging
import os
from glob import glob
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import Output, Run
from csm.models.output import OutputType
from csm.models.run import RunStatus
from csm.utils.time import now_utc_naive

# M4 TaskDefinition was retired in P2. `launch_run(task_def_id=...)` was
# removed 2026-07-25 — orchestrator.launch_mission(workflow_name, params)
# is the only supported automation entry point. Any caller still invoking
# `runner.launch_run(...)` will hit AttributeError, which is intentional.

log = logging.getLogger(__name__)


def _wrap_auto_prompt(
    task_name: str,
    triggered_at: str,
    trigger_kind: str,
    body: str,
) -> str:
    """System-built launch header for AUTO sessions.

    Gives the spawned agent context about WHY it's being woken up (manual
    one-shot vs cron tick vs scheduled at) before delivering the TaskDef's
    own prompt body.
    """
    return (
        f"[CSM automation trigger]\n"
        f"task: {task_name}\n"
        f"triggered at (UTC): {triggered_at}\n"
        f"trigger kind: {trigger_kind}\n"
        f"\n"
        f"---\n"
        f"\n"
        f"{body}"
    )


# Convention files — always scanned regardless of TaskDef output_globs.
CONVENTION_GLOBS = [
    "progress.json",
    ".progress.json",
    "state.json",
    "final_report.md",
    "summary.md",
    "report.md",
    "report.json",
    "journal*.jsonl",
    "_maintenance/progress.json",
    "_maintenance/final_report.md",
    "_maintenance/*_decisions.md",
]


class AutomationRunner:
    """Owns Run lifecycle: launch → wait for session.ended → record outputs.

    Contract for callers:
    - `start()` MUST be called before launching any run (subscribes to
      EventStream). `stop()` reverses it.
    - AUTO sessions are now spawned exclusively by
      `WorkflowOrchestrator.launch_mission(...)` (M4 TaskDefinition +
      `launch_run` were retired). This runner only owns the lifecycle
      of already-spawned AUTO sessions and finalizes Run rows on terminal
      events.
    - AUTO sessions don't self-exit when claude finishes (the TUI sits in the
      input box). The runner ends them on `MESSAGE_ASSISTANT_DONE` after a
      short grace, re-armed on every assistant turn so tool-use chains aren't
      cut short. A 10-minute hard timeout is a backstop for the case where
      `end_turn` never arrives.
    - On `SESSION_ENDED` / `SESSION_CRASHED`, the runner finalizes the Run
      and scans for outputs. If the in-memory `csm_session_id → run_id` map
      is empty (e.g. after a process restart), it falls back to a DB lookup
      so terminal events from pre-restart runs still complete.
    - Outputs are discovered via convention globs PLUS the TaskDef's
      `output_globs` field. **All globs are sandboxed to `TaskDef.cwd`** —
      absolute paths, `..`-traversal, and symlinks pointing outside cwd are
      rejected.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        event_stream: EventStream,
        session_manager: Any,
        grace_sec: float = 10.0,
        notification_bus: Any = None,
    ):
        self._sm = sessionmaker
        self._es = event_stream
        self._mgr = session_manager
        self._bus = notification_bus
        self._sub_id: str | None = None
        self._assistant_sub_id: str | None = None
        self._idle_sub_id: str | None = None
        self._runs_by_csm_session: dict[str, str] = {}  # csm_session_id → run_id
        # claude session uuid (JSONL filename) → csm session id. Needed because
        # MESSAGE_ASSISTANT_DONE events carry the claude id, not the csm id.
        self._csm_by_external_session: dict[str, str] = {}
        # run_id → pending grace timer task (one in-flight per Run).
        self._grace_tasks: dict[str, asyncio.Task] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._GRACE_SEC = grace_sec

    async def start(self) -> None:
        if self._sub_id is None:
            self._sub_id = self._es.subscribe(
                [EventType.SESSION_ENDED, EventType.SESSION_CRASHED],
                self._on_session_terminal,
            )
        if self._assistant_sub_id is None:
            self._assistant_sub_id = self._es.subscribe(
                [EventType.MESSAGE_ASSISTANT_DONE],
                self._on_assistant_done,
            )
        if self._idle_sub_id is None:
            self._idle_sub_id = self._es.subscribe(
                [EventType.SESSION_IDLE],
                self._on_session_idle,
            )
        # One-shot reap on startup catches Runs left RUNNING across a backend
        # restart (their PIDs are gone, but nothing has emitted SESSION_ENDED
        # for them).
        await self._reap_orphan_runs()
        # Periodic watchdog: if a claude PID dies WITHOUT the SessionManager
        # observing EOF on the PTY (e.g. external kill -9, OOM, crash), the
        # Run row would stay RUNNING forever. Re-scan every 60s.
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._orphan_watchdog_loop())

    async def stop(self) -> None:
        if self._sub_id is not None:
            self._es.unsubscribe(self._sub_id)
            self._sub_id = None
        if self._assistant_sub_id is not None:
            self._es.unsubscribe(self._assistant_sub_id)
            self._assistant_sub_id = None
        if self._idle_sub_id is not None:
            self._es.unsubscribe(self._idle_sub_id)
            self._idle_sub_id = None
        # Cancel any pending grace timers so they don't fire post-shutdown.
        for t in list(self._grace_tasks.values()):
            if not t.done():
                t.cancel()
        self._grace_tasks.clear()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None

    async def get_run(self, run_id: str) -> Run | None:
        async with self._sm() as db:
            return await db.get(Run, run_id)

    async def list_runs(self, limit: int = 100) -> list[Run]:
        # P2: `task_def_id` filter dropped — M4 is retired.
        async with self._sm() as db:
            stmt = select(Run).order_by(Run.started_at.desc()).limit(limit)
            return list((await db.execute(stmt)).scalars().all())

    async def cancel_run(self, run_id: str) -> Run:
        """User-initiated cancel: stop the AUTO session and mark Run as FAILED.

        Returns the updated Run row. Raises ValueError if the run doesn't
        exist or is already in a terminal state.
        """
        async with self._sm() as db:
            row = await db.get(Run, run_id)
            if row is None:
                raise ValueError(f"run {run_id} not found")
            if row.status != RunStatus.RUNNING:
                raise ValueError(f"run is {row.status.value}, not running")
            sid = row.session_id

        # Best-effort graceful stop of the underlying claude session. If the
        # session is already gone (PID reaped, race with watchdog), this is a
        # no-op — we still mark the Run as cancelled below.
        if sid:
            try:
                await self._mgr.stop_session(sid, graceful=True)
            except Exception:
                log.exception("cancel: stop_session(%s) failed", sid)

        # Mark Run as FAILED with sentinel exit_code so the UI can show
        # "cancelled" rather than a generic failure. EventStream's
        # SESSION_ENDED will also fire and re-finalize; that's idempotent.
        async with self._sm() as db:
            row = await db.get(Run, run_id)
            if row is None:
                raise ValueError(f"run {run_id} not found")
            if row.status == RunStatus.RUNNING:
                row.status = RunStatus.FAILED
                row.ended_at = now_utc_naive()
                if row.exit_code is None:
                    row.exit_code = -2  # convention: -2 = user cancelled
                await db.commit()
                await db.refresh(row)
            return row

    async def retry_run(self, run_id: str) -> Run:
        # P2: M4 TaskDefinition-based retry is retired. Workflow-mission
        # retries go through the orchestrator's `retry_from_stage`; direct
        # Run-level retry via this runner is no longer supported.
        raise NotImplementedError(
            "Run-level retry is retired (P2). Retry stages via "
            "orchestrator.retry_from_stage(mission_id, stage_name, mode)."
        )

    # ---- orphan Run reaper ----
    _ORPHAN_WATCHDOG_INTERVAL_SEC = 60.0

    async def _orphan_watchdog_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._ORPHAN_WATCHDOG_INTERVAL_SEC)
                await self._reap_orphan_runs()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("orphan watchdog failed (continuing)")

    # Finding-4 grace: mirrors the orchestrator's _REAP_GRACE_SEC. A short
    # CSM restart used to be indistinguishable from a real orphan because
    # PID was gone by the time we reaped. The grace window treats
    # "pid dead + last hook activity in the past 60s" as "probably a
    # restart victim, revisit next tick" rather than a terminal signal.
    _REAP_GRACE_SEC = 60.0

    async def _reap_orphan_runs(self) -> int:
        """Mark RUNNING Runs as FAILED when their session's PID is gone.

        Reasons a Run goes orphan:
          - Backend restarted while Run was in-flight (claude was killed by
            stop.sh's child-process sweep; no SESSION_ENDED ever emitted).
          - claude crashed in a way the PTY reader missed (rare).
          - External kill -9 on the spawned claude.

        Applies a 60s grace window keyed off `Session.last_activity_ts`
        (Finding-4): sessions that were freshly hooked less than a minute
        before reap are held in RUNNING and re-evaluated on the next
        watchdog tick, so a short CSM restart doesn't immediately
        clobber every in-flight mission's stage Run.

        Returns the number of rows reaped.
        """
        from csm.models.session import Session

        reaped = 0
        grace_skipped = 0
        now = now_utc_naive()
        async with self._sm() as db:
            # Mission-owned Run rows are the workflow orchestrator's
            # domain — its own `_startup_reap` + rescuer handle stuck
            # stage sessions. If runner reaps them here it stamps them
            # FAILED with exit=-1 even when the stage actually succeeded
            # and the mission has already advanced (the orchestrator's
            # `_stamp_stage_run_terminal` may not have run yet, or the
            # SESSION_ENDED handler is still in-flight). That was the
            # "5 stages show FAILED but mission is SUCCEEDED" bug.
            res = await db.execute(
                select(Run).where(
                    Run.status == RunStatus.RUNNING,
                    Run.mission_id.is_(None),
                )
            )
            runs = list(res.scalars().all())
            for r in runs:
                pid: int | None = None
                sess = None
                if r.session_id is not None:
                    sess = await db.get(Session, r.session_id)
                    if sess is not None:
                        pid = sess.pid
                # No PID known (session row missing or never bound) → orphan.
                # PID present but process is gone → orphan.
                if pid is None or not _pid_alive(pid):
                    if sess is not None:
                        marker = sess.last_activity_ts or sess.started_at
                        if marker is not None and (now - marker).total_seconds() < self._REAP_GRACE_SEC:
                            grace_skipped += 1
                            continue
                    r.status = RunStatus.FAILED
                    r.ended_at = now
                    if r.exit_code is None:
                        r.exit_code = -1
                    reaped += 1
            if reaped:
                await db.commit()
                log.info("reaped %d orphan Run(s)", reaped)
            if grace_skipped:
                log.info("reap grace-skipped %d Run(s) within %.0fs of last activity", grace_skipped, self._REAP_GRACE_SEC)
        return reaped

    # ---- completion via assistant_done ----
    async def _on_assistant_done(self, event: Event) -> None:
        """End the AUTO session N seconds after claude's last `end_turn`.

        Re-armed on every assistant turn — so a tool-use chain that produces
        multiple turns won't be cut short. The grace window swallows any
        bookkeeping turns that immediately follow the user-visible turn.
        """
        external_sid = event.session_id
        if not external_sid:
            return
        csm_sid = self._csm_by_external_session.get(external_sid)
        if csm_sid is None:
            return  # not one of our AUTO sessions
        run_id = self._runs_by_csm_session.get(csm_sid)
        if run_id is None:
            return  # already finalized
        prev = self._grace_tasks.pop(run_id, None)
        if prev is not None and not prev.done():
            prev.cancel()
        self._grace_tasks[run_id] = asyncio.create_task(
            self._grace_then_stop(csm_sid, run_id, self._GRACE_SEC),
            name=f"csm-auto-grace-{run_id}",
        )

    async def _on_session_idle(self, event: Event) -> None:
        """Backstop for the case where MESSAGE_ASSISTANT_DONE never fires.

        Symptom this catches: an AUTO claude session that either hangs in
        its REPL waiting for input, or crashes without emitting a terminal
        JSONL record, or gets stuck in a network-bound tool_use that never
        returns. `_on_assistant_done` needs an `end_turn` event that never
        arrives, so the grace timer stays unarmed. Meanwhile
        `_on_session_terminal` needs SESSION_ENDED which is PTY-EOF driven
        — no help if the child is alive but silent.

        SESSION_IDLE fires from EventStream's watchdog when the child's
        JSONL has been quiet for `session_idle_minutes` (30 min by
        default). For an AUTO session that's a definitive "stuck" signal:
        force stop, mark the Run FAILED with a clear reason.
        """
        external_sid = event.session_id
        idle_seconds = (event.payload or {}).get("idle_seconds", 0)
        # Finding-6 tracing: log every entry regardless of match outcome, so
        # csm.log tells us whether the handler was reached and (if it exits
        # early) which lookup missed. Previously silent early-returns made
        # it impossible to distinguish "handler never called" from "handler
        # called but session wasn't ours".
        log.info(
            "runner _on_session_idle entered: external_sid=%s idle=%ss known_auto=%s",
            external_sid,
            idle_seconds,
            external_sid in self._csm_by_external_session if external_sid else False,
        )
        if not external_sid:
            return
        csm_sid = self._csm_by_external_session.get(external_sid)
        if csm_sid is None:
            return  # not one of our AUTO sessions
        run_id = self._runs_by_csm_session.get(csm_sid)
        if run_id is None:
            return  # already finalized
        log.warning(
            "run %s: AUTO session idle %ds; force-stopping (backstop for missing end_turn)",
            run_id,
            idle_seconds,
        )
        # Cancel any pending grace timer so we don't double-stop.
        pending = self._grace_tasks.pop(run_id, None)
        if pending is not None and not pending.done():
            pending.cancel()
        await self._stop_session_safe(csm_sid)

    async def _grace_then_stop(self, csm_sid: str, run_id: str, grace_sec: float) -> None:
        try:
            await asyncio.sleep(grace_sec)
        except asyncio.CancelledError:
            return
        # Re-check: terminal handler might have fired while we slept; or a
        # newer assistant_done might have replaced us with a fresh task.
        if self._grace_tasks.get(run_id) is not asyncio.current_task():
            return
        if csm_sid not in self._runs_by_csm_session:
            return
        self._grace_tasks.pop(run_id, None)
        log.info(
            "run %s: %.1fs grace after end_turn elapsed; stopping session %s",
            run_id,
            grace_sec,
            csm_sid,
        )
        await self._stop_session_safe(csm_sid)

    async def _stop_session_safe(self, csm_sid: str) -> None:
        try:
            await self._mgr.stop_session(csm_sid, graceful=True)
        except Exception:
            log.exception("failed to stop auto session %s", csm_sid)

    # ---- termination handling ----
    async def _on_session_terminal(self, event: Event) -> None:
        csm_sid = (event.payload or {}).get("csm_session_id")
        if not csm_sid:
            return
        # Try in-memory map first (fast path).
        run_id = self._runs_by_csm_session.pop(csm_sid, None)
        if run_id is None:
            # Restart-recovery path: the map didn't survive a backend restart,
            # but Run.session_id was persisted in P1 — look it up.
            #
            # Mission-owned Run rows (mission_id IS NOT NULL) are OWNED by the
            # workflow orchestrator's own SESSION_ENDED subscriber. If runner
            # picks them up here it will (a) mark them SUCCEEDED via
            # `_finalize_run` and (b) NULL out `Run.session_id` in
            # `_purge_auto_session_row`, which then makes the orchestrator's
            # `select(Run).where(session_id == csm_sid)` lookup miss and the
            # rescuer subsequently flip the mission to FAILED with
            # "succeeded run has no session_id". Skip them here.
            async with self._sm() as db:
                res = await db.execute(
                    select(Run).where(
                        Run.session_id == csm_sid,
                        Run.status == RunStatus.RUNNING,
                        Run.mission_id.is_(None),
                    )
                )
                row = res.scalar_one_or_none()
                if row is None:
                    return
                run_id = row.id
        # Cancel any pending grace timer before finalization — otherwise it
        # would fire post-finalize and try to stop an already-stopped session.
        grace = self._grace_tasks.pop(run_id, None)
        if grace is not None and not grace.done():
            grace.cancel()
        # Drop the claude→csm reverse mapping (linear scan, small dict).
        for external_sid, csid in list(self._csm_by_external_session.items()):
            if csid == csm_sid:
                self._csm_by_external_session.pop(external_sid, None)
        exit_code = event.payload.get("exit_code")
        await self._finalize_run(run_id, exit_code)

    async def _finalize_run(self, run_id: str, exit_code: int | None) -> None:
        session_id_to_purge: str | None = None
        async with self._sm() as db:
            run = await db.get(Run, run_id)
            if run is None:
                return
            session_id_to_purge = run.session_id
            run.ended_at = now_utc_naive()
            run.exit_code = exit_code
            run.status = RunStatus.SUCCEEDED if (exit_code in (None, 0)) else RunStatus.FAILED
            await db.commit()
        # P2: TaskDefinition-based output_globs discovery is retired.
        # Workflow Runs discover outputs via the orchestrator's stage
        # validation path; this branch used to serve M4 task runs only.
        # Auto-cleanup the AUTO session row so the Sessions UI doesn't
        # accumulate exited/crashed automation sessions forever. The PTY
        # child is already dead by the time we get here (SESSION_ENDED
        # fired). We only need to drop the DB row + notifications and
        # null out the back-reference on this Run.
        if session_id_to_purge:
            await self._purge_auto_session_row(session_id_to_purge, run_id)

    async def _purge_auto_session_row(self, session_id: str, run_id: str) -> None:
        """Delete a finalized AUTO session's DB row so the UI doesn't show
        stale exited/crashed sessions after their Run is done. Mirrors the
        `/sessions/{sid}/purge` API path but without the process-kill step
        (SESSION_ENDED already means the process is gone).
        """
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import update as sa_update

        from csm.models import Notification
        from csm.models import Run as _Run
        from csm.models import Session as _SessionModel
        try:
            async with self._sm() as db:
                # Defensive: only purge sessions of type AUTO.
                sess = await db.get(_SessionModel, session_id)
                if sess is None:
                    return
                sess_type = sess.type.value if hasattr(sess.type, "value") else sess.type
                if sess_type != "auto":
                    return
                # Null out session_id on any Run(s) that reference this session
                # so foreign-key style joins don't dangle.
                await db.execute(
                    sa_update(_Run)
                    .where(_Run.session_id == session_id)
                    .values(session_id=None)
                )
                await db.execute(
                    sa_delete(Notification).where(
                        Notification.session_id == session_id
                    )
                )
                await db.execute(
                    sa_delete(_SessionModel).where(_SessionModel.id == session_id)
                )
                await db.commit()
            # Evict in-memory dedup state on the bus so `_last_new_msg`
            # / `_session_locks` / `_last_assistant_done_bump` don't
            # accumulate dangling entries for the now-deleted session.
            # Mirrors what `POST /sessions/{sid}/purge` does at the API
            # layer; without this, per-session dicts grow monotonically.
            if self._bus is not None:
                try:
                    self._bus.evict_session(session_id)
                except Exception:
                    log.exception(
                        "runner: bus.evict_session(%s) failed after purging auto session",
                        session_id,
                    )
            log.info(
                "runner: purged auto session %s after finalizing run %s",
                session_id, run_id,
            )
        except Exception:
            log.exception(
                "runner: purge auto session %s failed (run %s)",
                session_id, run_id,
            )

    async def _discover_outputs(self, run_id: str, cwd: str, extra_globs: list[str]) -> None:
        if not cwd:
            return
        cwd_resolved = Path(cwd).resolve()
        # Drop any pattern that's absolute or escapes cwd via .. — these
        # would let a malicious / sloppy TaskDef leak arbitrary files into
        # Run.outputs (and back out via /api/runs/{id}).
        safe_extras: list[str] = []
        for p in extra_globs or []:
            if Path(p).is_absolute() or ".." in Path(p).parts:
                log.warning("rejecting unsafe output_glob %r (run %s)", p, run_id)
                continue
            safe_extras.append(p)
        patterns = list(CONVENTION_GLOBS) + safe_extras
        seen: set[str] = set()
        rows: list[Output] = []
        for pattern in patterns:
            for path in await asyncio.to_thread(_glob_in, cwd, pattern):
                if path in seen:
                    continue
                seen.add(path)
                # Containment check: resolved file MUST stay under cwd.
                try:
                    rp = Path(path).resolve()
                    if not rp.is_relative_to(cwd_resolved):
                        log.warning("output %s escapes cwd %s; skipping", path, cwd_resolved)
                        continue
                    if Path(path).is_symlink():
                        log.warning("output %s is a symlink; skipping (no leak)", path)
                        continue
                except (OSError, ValueError):
                    continue
                preview = _safe_preview(path, max_bytes=4096)
                rows.append(
                    Output(
                        run_id=run_id,
                        path=path,
                        type=_classify(path),
                        preview=preview,
                    )
                )
        if not rows:
            return
        async with self._sm() as db:
            for r in rows:
                db.add(r)
            await db.commit()


def _pid_alive(pid: int) -> bool:
    """True if `pid` refers to a live process. Duplicated from session_manager
    to avoid a cross-module import cycle (manager already depends on runner
    indirectly via app.state wiring)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _glob_in(cwd: str, pattern: str) -> list[str]:
    full = os.path.join(cwd, pattern)
    return sorted(glob(full, recursive=True))


def _classify(path: str) -> OutputType:
    lower = path.lower()
    if lower.endswith(".md"):
        return OutputType.MARKDOWN
    if lower.endswith(".json") or lower.endswith(".jsonl"):
        return OutputType.JSON
    if lower.endswith(".log"):
        return OutputType.LOG
    return OutputType.FILE


def _safe_preview(path: str, max_bytes: int = 4096) -> str | None:
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None

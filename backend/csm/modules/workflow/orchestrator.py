"""WorkflowOrchestrator — Mission state machine (M8 night2 T1-T7).

T1 introduced the skeleton (deps + start/stop). T2 added the Mission
lifecycle methods and the status-transition table. T3 wired the
event-driven advancement (`SESSION_ENDED` → validate → next stage / end).
T4 lifts the renderer to the PRD-complete vocabulary and starts writing
a `STATE.yaml` snapshot. T5 wires in the `PollExecutor` so a `poll`-kind
next stage no longer dead-ends the Mission: it spawns an asyncio loop
that runs the stage's `check:` block until pass / timeout. T6 adds a
30s cron rescuer (`_rescuer_loop` / `_rescue_pass`) that handles lost
SESSION_ENDED events — the edge case where `stop_session` failed AND
the JSONL tail missed the assistant_done — by reconciling Mission rows
against the live state of their stage Run + Session rows + OS PID. T7
adds two more safety nets on top of the rescuer:

1. **Mission timeout** — every pass first checks `mission.started_at +
   spec.global_timeout < now`. If true, the rescuer cancels any live
   poll loop owned by the mission, sends `stop_session` to the current
   claude stage's PID (if alive), and finalises the mission FAILED with
   reason `"mission timed out at <duration>s"`. `global_timeout` is read
   off the workflow YAML (PRD §3, default 604800s == 7 days), so missions
   from a spec that doesn't declare a tighter budget effectively have no
   timeout under realistic runtimes.
2. **Startup reap** — `start()` runs `_startup_reap` exactly once before
   spawning the periodic loop. The reap scans every RUNNING mission for
   the strict orphan-PID case (Run.status RUNNING + Session.pid dead) and
   finalises each FAILED with reason `"reaped on startup"`. This catches
   missions whose stage PID died while the backend itself was offline —
   without the reap they'd sit RUNNING until the first 30s tick.

Run-state decision table (T6, unchanged):

| Run state                      | Action                           |
|--------------------------------|----------------------------------|
| missing                        | fail Mission ("no run for stage")|
| RunStatus.SUCCEEDED            | dispatch synthetic SESSION_ENDED |
|                                | (validation + advance)           |
| RunStatus.FAILED               | fail Mission (run reported fail) |
| RunStatus.RUNNING + PID alive  | leave alone                      |
| RunStatus.RUNNING + PID dead   | fail Mission ("orphaned PID")    |

Per-mission errors are caught so one bad mission doesn't poison the
pass. Top-level errors (e.g. unreachable DB) are swallowed too — the
rescuer must never crash the orchestrator's lifespan.

Out of scope (later tasks):
- Restart-time `PollExecutor.start_poll` for live poll stages (T7 deliberately
  defers this — startup reap only handles the claude branch since poll stages
  don't currently allocate Run.session_id).
- `stop_reason` guard tinkering — that's the night1 core fix and the
  night_plan forbids touching it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.config import settings
from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import (
    Mission,
    MissionStatus,
    Output,
    Run,
    Session,
    WorkflowDefinition,
)
from csm.models.run import RunStatus
from csm.models.session import SessionType
from csm.modules.automation.runner import _classify as _classify_output_type
from csm.modules.automation.runner import _safe_preview
from csm.modules.workflow import orchestrator_reaper as _reaper
from csm.modules.workflow import orchestrator_state as _state_yaml
from csm.modules.workflow.engine import validate_stage
from csm.modules.workflow.poll_executor import PollExecutor
from csm.modules.workflow.rendering import PromptRenderError, render_prompt
from csm.modules.workflow.schema import StageSpec, WorkflowSpec, load_workflow_spec
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)


class InvalidMissionStateTransition(Exception):
    """Raised when a caller requests a transition not in `_VALID_TRANSITIONS`.

    Message always includes the mission id and both states so the operator
    can trace which API call attempted the illegal move.
    """


# PRD §5 transition table. Terminal states intentionally have no outbound
# edges — re-running a successful or cancelled mission is a *new* mission.
# `paused` is currently a write-only state (no caller yet, but allowed for
# forward-compat per PRD note).
_VALID_TRANSITIONS: dict[MissionStatus, set[MissionStatus]] = {
    MissionStatus.PENDING: {MissionStatus.RUNNING},
    MissionStatus.RUNNING: {
        MissionStatus.SUCCEEDED,
        MissionStatus.FAILED,
        MissionStatus.CANCELLED,
        MissionStatus.PAUSED,
    },
    MissionStatus.FAILED: {MissionStatus.RUNNING},
    MissionStatus.PAUSED: {MissionStatus.RUNNING, MissionStatus.CANCELLED},
    MissionStatus.SUCCEEDED: set(),
    MissionStatus.CANCELLED: set(),
}


# STATE.yaml constants live in orchestrator_state now; re-exported for any
# external code that used to import `_STATE_YAML_FILENAME` from this module.
_STATE_YAML_FILENAME = _state_yaml.STATE_YAML_FILENAME
_STATE_YAML_HEADER = _state_yaml.STATE_YAML_HEADER


class WorkflowOrchestrator:
    # T6 rescuer cadence — see `settings.rescue_interval_sec` (env-overridable
    # via `CSM_RESCUE_INTERVAL_SEC`). Kept as a property so existing tests /
    # callers reading `WorkflowOrchestrator._RESCUE_INTERVAL_SEC` still work.
    @property
    def _RESCUE_INTERVAL_SEC(self) -> float:  # noqa: N802
        return settings.rescue_interval_sec

    # Finding-4 (test-run-1): a short CSM restart used to instantly fail
    # every in-flight mission because the startup reap saw pid-dead sessions
    # and immediately called it orphaned. Grace window: a session whose
    # last recorded hook activity is within this many seconds of "now" is
    # assumed to be a restart victim, not a real crash. Subsequent rescuer
    # ticks re-evaluate; the grace only defers, it never absolves.
    _REAP_GRACE_SEC = 60.0

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        event_stream: EventStream,
        session_manager: Any,
        runner: Any,
        workflow_loader: Any,
        *,
        poll_executor: PollExecutor | None = None,
    ):
        self._sm = sessionmaker
        self._es = event_stream
        self._session_manager = session_manager
        self._runner = runner
        self._workflow_loader = workflow_loader
        self._subscription_id: str | None = None
        # Inject-able for tests; default wiring binds the executor's
        # pass/fail callbacks to this orchestrator's advancement /
        # failure paths so the executor stays decoupled from Mission
        # state-machine internals.
        self._poll_executor: PollExecutor = poll_executor or PollExecutor(
            sessionmaker,
            on_pass=self._on_poll_pass,
            on_fail=self._on_poll_fail,
        )
        # T6 rescuer lifecycle. `_stopping` is constructed lazily in
        # `start()` because the running event loop must own the Event.
        self._stopping: asyncio.Event | None = None
        self._rescuer_task: asyncio.Task | None = None
        # Finding-8 (test-run-2): claude REPL never self-exits after an
        # assistant end_turn. Runner handles this for its own launched
        # runs via MESSAGE_ASSISTANT_DONE → grace → stop_session, but
        # orchestrator-owned stage sessions bypass runner's map and hang
        # until global_timeout. We now drive the same lifecycle for
        # orchestrator sessions: track external_sid → csm_sid, arm a grace
        # timer on assistant_done, force stop_session at grace end so
        # PTY EOF → SESSION_ENDED → validate + advance.
        self._orch_csm_by_external_session: dict[str, str] = {}
        self._orch_grace_tasks: dict[str, asyncio.Task] = {}
        self._orch_assistant_sub_id: str | None = None
        self._orch_idle_sub_id: str | None = None
        self._orch_grace_sec: float = 60.0

    async def start(self) -> None:
        if self._subscription_id is None:
            self._subscription_id = self._es.subscribe(
                [EventType.SESSION_ENDED], self._on_session_ended
            )
        # Finding-8: drive orchestrator stage lifecycle from assistant end_turn.
        if self._orch_assistant_sub_id is None:
            self._orch_assistant_sub_id = self._es.subscribe(
                [EventType.MESSAGE_ASSISTANT_DONE], self._on_stage_assistant_done
            )
        # Finding-8b (TR2 follow-up): claude may terminate without ever
        # emitting `end_turn` — rate limit rejection, network crash,
        # process kill by external OOM. In that case MESSAGE_ASSISTANT_DONE
        # never fires and the stage session sits alive until global_timeout.
        # SESSION_IDLE (30-min silent JSONL) is a strong last-resort signal
        # that this session is done, whatever the reason.
        if self._orch_idle_sub_id is None:
            self._orch_idle_sub_id = self._es.subscribe(
                [EventType.SESSION_IDLE], self._on_stage_session_idle
            )
        # T7: one-shot orphan-PID reap before the periodic rescuer fires.
        # Cleans up missions whose stage PID died while the backend was
        # down so they don't sit RUNNING for ~30s waiting for the first
        # tick. Wrapped in try/except inside `_startup_reap` itself, so a
        # broken sessionmaker (smoke test) doesn't abort the start.
        await self._startup_reap()
        # Spawn the rescuer in the current loop. The loop self-exits when
        # `_stopping` is set, so `stop()` doesn't need to cancel directly.
        if self._rescuer_task is None or self._rescuer_task.done():
            self._stopping = asyncio.Event()
            self._rescuer_task = asyncio.create_task(
                self._rescuer_loop(), name="workflow-orchestrator-rescuer"
            )
        log.info("WorkflowOrchestrator started")

    async def stop(self) -> None:
        if self._subscription_id is not None:
            self._es.unsubscribe(self._subscription_id)
            self._subscription_id = None
        if self._orch_assistant_sub_id is not None:
            self._es.unsubscribe(self._orch_assistant_sub_id)
            self._orch_assistant_sub_id = None
        if self._orch_idle_sub_id is not None:
            self._es.unsubscribe(self._orch_idle_sub_id)
            self._orch_idle_sub_id = None
        for t in list(self._orch_grace_tasks.values()):
            if not t.done():
                t.cancel()
        self._orch_grace_tasks.clear()
        # Signal the rescuer to stop and await it before tearing down the
        # poll executor. A cancel fallback covers the unlikely case where
        # the loop's body is stuck in a non-event-driven coro.
        if self._stopping is not None:
            self._stopping.set()
        if self._rescuer_task is not None:
            try:
                await asyncio.wait_for(self._rescuer_task, timeout=5.0)
            except TimeoutError:
                self._rescuer_task.cancel()
                try:
                    await self._rescuer_task
                except (asyncio.CancelledError, Exception):
                    pass
            except (asyncio.CancelledError, Exception):
                pass
            self._rescuer_task = None
        # Cancel any live poll loops so they don't keep ticking past
        # the FastAPI lifespan shutdown.
        await self._poll_executor.stop()
        log.info("WorkflowOrchestrator stopped")

    # ------------------------------------------------------------------
    # Mission lifecycle
    # ------------------------------------------------------------------

    async def launch_mission(
        self, workflow_name: str, params: dict[str, Any]
    ) -> Mission:
        """Create a Mission row in `running` for the named workflow.

        T3 scope: writes the DB row, creates the workspace directory, AND
        spawns the first claude stage's AUTO session. The `SESSION_ENDED`
        subscription will then carry the Mission forward stage by stage.
        """
        async with self._sm() as db:
            wf_row = await db.scalar(
                select(WorkflowDefinition).where(WorkflowDefinition.name == workflow_name)
            )
            if wf_row is None:
                raise ValueError(f"unknown workflow {workflow_name!r}")

            spec = load_workflow_spec(wf_row.yaml_content)
            # Populate any params whose spec declares a default BEFORE
            # validation runs — otherwise a workflow's `default:` field is
            # useless and every caller has to redundantly repeat it. Copy
            # so we don't mutate the caller's dict.
            params = dict(params)
            for p in spec.parameters:
                if p.name not in params and p.default is not None:
                    params[p.name] = p.default
            self._validate_params(spec, params)

            mission_id = str(uuid.uuid4())
            workspace_path = self._resolve_workspace(spec.workspace, mission_id)
            Path(workspace_path).mkdir(parents=True, exist_ok=True)

            mission = Mission(
                id=mission_id,
                workflow_def_id=wf_row.id,
                parameters=dict(params),
                workspace_path=workspace_path,
                status=MissionStatus.RUNNING,
                current_stage=spec.stages[0].name,
                started_at=now_utc_naive(),
                audit_log=[
                    {
                        "ts": _iso_now(),
                        "event": "launched",
                        "from": MissionStatus.PENDING.value,
                        "to": MissionStatus.RUNNING.value,
                    }
                ],
            )
            db.add(mission)
            await db.commit()
            await db.refresh(mission)
            log.info(
                "launched mission %s workflow=%s stage=%s ws=%s",
                mission.id,
                workflow_name,
                mission.current_stage,
                workspace_path,
            )

        # Snapshot the initial state to <ws>/STATE.yaml before any stage
        # spawns — the spawned claude session inherits a workspace where
        # this file already exists.
        await self._write_state_yaml(mission.id)

        # First stage is always live as soon as the row exists. T2 stopped
        # short here; T3 spawns the AUTO session; T5 starts a poll loop
        # when the first stage is a poll-kind.
        first_stage = spec.stages[0]
        if first_stage.kind == "claude":
            await self._start_claude_stage(mission, first_stage)
        else:
            await self._poll_executor.start_poll(
                mission.id, first_stage.name, first_stage
            )
        return mission

    async def cancel_mission(self, mission_id: str) -> Mission:
        """Transition a `running` (or `paused`) mission to `cancelled`."""
        async with self._sm() as db:
            mission = await db.get(Mission, mission_id)
            if mission is None:
                raise ValueError(f"unknown mission {mission_id!r}")
            self._check_transition(mission, MissionStatus.CANCELLED)
            prev = mission.status
            mission.status = MissionStatus.CANCELLED
            mission.ended_at = now_utc_naive()
            mission.audit_log = (mission.audit_log or []) + [
                {
                    "ts": _iso_now(),
                    "event": "cancelled",
                    "from": prev.value,
                    "to": MissionStatus.CANCELLED.value,
                }
            ]
            await db.commit()
            await db.refresh(mission)
        # Stop any live poll loops for this mission before snapshotting
        # STATE.yaml — once cancelled, the loop should not produce more
        # on_pass / on_fail callbacks that would try to mutate the row.
        await self._poll_executor.cancel_all_for_mission(mission_id)
        await self._write_state_yaml(mission_id)
        log.info("cancelled mission %s (was %s)", mission_id, prev.value)
        return mission

    async def retry_from_stage(
        self, mission_id: str, stage_name: str, *, mode: str = "rerun"
    ) -> Mission:
        """Transition `failed` → `running`, rewinding `current_stage`.

        `stage_name` must be a stage declared on the mission's workflow
        spec; otherwise raises `ValueError`. Callers that want to "resume
        where it stopped" should pass the stored `current_stage` value.

        `mode` controls what happens **after** the state transition:

        - `rerun` (default): spawn a fresh AUTO session (claude stage) or
          new poll loop (poll stage) — the stage is executed from scratch,
          overwriting whatever files may have been produced last time.
          Fits the "the stage crashed / claude gave up, try again" story.

        - `revalidate`: skip spawning; re-run the stage's validation
          against the *current* workspace state. If validation passes, the
          Mission advances to the next stage exactly as it would have if
          the first run had succeeded. If validation still fails, the
          Mission drops back to `failed` with the fresh failure reason.
          Fits the "I manually fixed the output file, now re-check it"
          story — the more common real-world manual-recovery flow.

        `revalidate` is only meaningful for `claude` stages that declared
        a `validation` block. On `poll` stages or claude stages without
        validation it degrades to a no-op advance (the stage's outputs
        are already the source of truth; there's nothing to re-check).
        """
        if mode not in ("rerun", "revalidate"):
            raise ValueError(
                f"invalid retry mode {mode!r}; expected 'rerun' or 'revalidate'"
            )
        async with self._sm() as db:
            mission = await db.get(Mission, mission_id)
            if mission is None:
                raise ValueError(f"unknown mission {mission_id!r}")
            self._check_transition(mission, MissionStatus.RUNNING)

            wf_row = await db.get(WorkflowDefinition, mission.workflow_def_id)
            if wf_row is None:
                raise ValueError(
                    f"workflow row {mission.workflow_def_id!r} missing for mission {mission_id!r}"
                )
            spec = load_workflow_spec(wf_row.yaml_content)
            stage_names = {s.name for s in spec.stages}
            if stage_name not in stage_names:
                raise ValueError(
                    f"stage {stage_name!r} not declared on workflow {spec.name!r}; "
                    f"known stages: {sorted(stage_names)}"
                )

            prev = mission.status
            mission.status = MissionStatus.RUNNING
            mission.current_stage = stage_name
            mission.ended_at = None
            mission.failure_reason = None
            mission.audit_log = (mission.audit_log or []) + [
                {
                    "ts": _iso_now(),
                    "event": "retried",
                    "from": prev.value,
                    "to": MissionStatus.RUNNING.value,
                    "stage": stage_name,
                }
            ]
            await db.commit()
            await db.refresh(mission)
        await self._write_state_yaml(mission_id)
        log.info(
            "retried mission %s from stage=%s (was %s)",
            mission_id,
            stage_name,
            prev.value,
        )
        _, stage_spec = _find_stage(spec, stage_name)
        if stage_spec is None:
            return mission

        if mode == "rerun":
            # Re-spawn the stage — otherwise the Mission sits in `running` with
            # no live Run and the rescuer eventually reaps it as orphaned.
            if stage_spec.kind == "claude":
                await self._start_claude_stage(mission, stage_spec)
            else:
                await self._poll_executor.start_poll(
                    mission_id, stage_name, stage_spec
                )
            return mission

        # mode == "revalidate": re-check the current workspace state
        # against this stage's compiled rules without spawning anything.
        # Success → advance to next stage. Failure → mission falls back
        # to `failed` with the fresh reason.
        await self._revalidate_stage(mission, stage_spec, spec, wf_row)
        # Return the freshest Mission row so the caller sees the resolved state.
        return await self.get_mission(mission_id) or mission

    async def _revalidate_stage(
        self,
        mission: Mission,
        stage_spec: StageSpec,
        spec: WorkflowSpec,
        wf_row: WorkflowDefinition,
    ) -> None:
        """Re-run stage validation without spawning; advance or re-fail.

        The stage's compiled validation rules live in
        `WorkflowDefinition.compiled_rules`. We reuse `validate_stage` so
        the result is identical to what the SESSION_ENDED subscriber
        would compute after a real run.
        """
        compiled = wf_row.compiled_rules or {}
        stage_compiled = (compiled.get("stages") or {}).get(stage_spec.name) or {
            "kind": stage_spec.kind,
            "validation": [],
        }
        async with self._sm() as db:
            prior_outputs = await self._collect_prior_outputs(
                db, mission_id=mission.id, before_stage=stage_spec.name, spec=spec
            )
        report = validate_stage(
            stage_compiled=stage_compiled,
            workspace_path=mission.workspace_path,
            params=dict(mission.parameters or {}),
            prior_outputs=prior_outputs,
        )
        if not report.get("pass", False):
            failed = report.get("failed_checks") or []
            reason = f"stage {stage_spec.name!r} revalidation failed: {failed}"
            await self._finalize_mission_failed(mission.id, reason)
            return

        # Revalidation passed. Persist Output rows against a placeholder
        # Run row so subsequent stages' `{stages.X.outputs[N]}`
        # references resolve. Reuses the same _persist_stage_outputs
        # helper used after a real session ends — but binds to a
        # freshly-created Run with status SUCCEEDED so the audit trail
        # doesn't lose the fact that this stage was revalidated.
        async with self._sm() as db:
            run = Run(
                session_id=None,
                status=RunStatus.SUCCEEDED,
                started_at=now_utc_naive(),
                ended_at=now_utc_naive(),
                exit_code=0,
                parameters=dict(mission.parameters or {}),
                mission_id=mission.id,
                stage_name=stage_spec.name,
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)
            run_id = run.id
        await self._persist_stage_outputs(
            run_id=run_id,
            mission_id=mission.id,
            workspace_path=mission.workspace_path,
            workflow_name=spec.name,
            params=dict(mission.parameters or {}),
            prior_outputs=prior_outputs,
            stage_spec=stage_spec,
        )
        await self._advance_to_next_stage(mission.id, stage_spec.name, spec)

    async def get_mission(self, mission_id: str) -> Mission | None:
        async with self._sm() as db:
            return await db.get(Mission, mission_id)

    # ------------------------------------------------------------------
    # Stage execution (T3)
    # ------------------------------------------------------------------

    async def _start_claude_stage(
        self, mission: Mission, stage_spec: StageSpec
    ) -> None:
        """Spawn the AUTO session for `stage_spec` and record a Run row.

        The Run row is stamped with `mission_id` + `stage_name` so the
        cron rescuer (T6) and timeout reaper (T7) can find orphaned stage
        runs by querying `Run.mission_id IS NOT NULL`.
        """
        # Pull the workflow spec + prior-stage outputs before rendering
        # so cross-stage placeholders (`{stages.X.outputs[N]}`) resolve
        # to the paths the upstream stage actually emitted.
        async with self._sm() as db:
            wf_row = await db.get(WorkflowDefinition, mission.workflow_def_id)
            if wf_row is None:
                raise ValueError(
                    f"mission {mission.id!r} references missing workflow {mission.workflow_def_id!r}"
                )
            spec = load_workflow_spec(wf_row.yaml_content)
            prior_outputs = await self._collect_prior_outputs(
                db, mission_id=mission.id, before_stage=stage_spec.name, spec=spec
            )

        try:
            rendered_prompt = render_prompt(
                stage_spec.prompt or "",
                params=dict(mission.parameters or {}),
                workspace_path=mission.workspace_path,
                mission_id=mission.id,
                workflow_name=spec.name,
                stage_outputs=prior_outputs,
            )
        except PromptRenderError as e:
            log.warning(
                "mission %s stage %s prompt rendering failed: %s",
                mission.id,
                stage_spec.name,
                e,
            )
            await self._finalize_mission_failed(
                mission.id,
                f"stage {stage_spec.name!r} prompt render failed: {e}",
            )
            return

        async with self._sm() as db:
            run = Run(
                session_id=None,
                status=RunStatus.RUNNING,
                parameters=dict(mission.parameters or {}),
                mission_id=mission.id,
                stage_name=stage_spec.name,
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)
            run_id = run.id

        # Refresh STATE.yaml so the file mirrors "this stage is running"
        # *before* the AUTO claude opens it. The session inherits a workspace
        # in which STATE.yaml already shows current_stage = stage_spec.name.
        await self._write_state_yaml(mission.id)

        # Multi-agent v2: pick which CLI-adapter runs this stage.
        # Precedence: stage.agent > workflow.default_agent > user default.
        # `getattr` on the pydantic StageSpec + workflow spec keeps this
        # forward-compatible with schema versions that haven't added the
        # fields yet.
        stage_agent = getattr(stage_spec, "agent", None)
        workflow_default = getattr(spec, "default_agent", None)
        user_default = await self._resolve_user_default_agent()
        effective_agent = stage_agent or workflow_default or user_default

        sess = await self._session_manager.create_session(
            cwd=mission.workspace_path,
            type=SessionType.AUTO,
            title=f"mission:{mission.id[:8]}:{stage_spec.name}",
            initial_prompt=rendered_prompt,
            run_id=run_id,
            agent=effective_agent,
        )

        async with self._sm() as db:
            row = await db.get(Run, run_id)
            if row is not None:
                row.session_id = sess.id
                await db.commit()

        # Finding-8: register external_session_id → csm_session_id so
        # `_on_stage_assistant_done` can find this session when the
        # MESSAGE_ASSISTANT_DONE event carries the claude id.
        if sess.external_session_id:
            self._orch_csm_by_external_session[sess.external_session_id] = sess.id

        log.info(
            "mission %s: started stage %s (run=%s, csm_session=%s)",
            mission.id,
            stage_spec.name,
            run_id,
            sess.id,
        )

    async def _on_stage_assistant_done(self, event: Event) -> None:
        """Finding-8: force-stop a stage's AUTO session N seconds after
        claude's last `end_turn`.

        Claude REPL never self-exits after producing its final answer, so
        the stage session sits alive forever and the mission never
        advances until global_timeout fires. Mirrors the runner's
        MESSAGE_ASSISTANT_DONE → grace → stop_session flow but scoped to
        orchestrator-owned sessions (runner's flow ignores them because
        they aren't in runner's `_csm_by_external_session` map).

        Grace is re-armed on every assistant turn so a tool-use chain
        that produces multiple end_turns won't be cut short — the last
        end_turn wins.
        """
        try:
            external_sid = event.session_id
            if not external_sid:
                return
            csm_sid = self._orch_csm_by_external_session.get(external_sid)
            if csm_sid is None:
                return  # not one of our mission stage sessions
            prev = self._orch_grace_tasks.pop(csm_sid, None)
            if prev is not None and not prev.done():
                prev.cancel()
            log.info(
                "mission stage assistant end_turn: arming %.1fs grace for csm_session=%s",
                self._orch_grace_sec,
                csm_sid,
            )
            self._orch_grace_tasks[csm_sid] = asyncio.create_task(
                self._orch_grace_then_stop(csm_sid, self._orch_grace_sec),
                name=f"orch-grace-{csm_sid}",
            )
        except Exception:
            log.exception("orchestrator._on_stage_assistant_done failed")

    async def _on_stage_session_idle(self, event: Event) -> None:
        """Finding-8b: last-resort force-stop for a stage session whose claude
        never emitted `end_turn`.

        Reasons this happens in the wild:
          - claude hit a rate limit and printed an error message then
            hung waiting for the user to retry (no assistant_done).
          - claude subprocess got OOM-killed or SIGSEGV'd; JSONL stops
            growing but the PTY child is a zombie.
          - Network hang on a tool_use with no timeout (very rare).

        When the watchdog decides a session has been silent for
        `session_idle_minutes` (30 by default), we treat that as "the
        session is definitely done" and stop it. Symmetric with
        `AutomationRunner._on_session_idle` but scoped to orchestrator
        sessions (runner's handler ignores us because we're not in its
        map).
        """
        try:
            external_sid = event.session_id
            if not external_sid:
                return
            csm_sid = self._orch_csm_by_external_session.get(external_sid)
            if csm_sid is None:
                return  # not one of our stage sessions
            idle_seconds = (event.payload or {}).get("idle_seconds", 0)
            log.warning(
                "mission stage session idle %ds; force-stopping csm_session=%s "
                "(backstop for missing end_turn — rate limit / crash / hung tool)",
                idle_seconds,
                csm_sid,
            )
            # Cancel any pending grace timer so we don't double-stop.
            pending = self._orch_grace_tasks.pop(csm_sid, None)
            if pending is not None and not pending.done():
                pending.cancel()
            try:
                await self._session_manager.stop_session(csm_sid, graceful=True)
            except Exception:
                log.exception("orchestrator idle stop_session failed csm_session=%s", csm_sid)
        except Exception:
            log.exception("orchestrator._on_stage_session_idle failed")

    async def _orch_grace_then_stop(self, csm_sid: str, grace_sec: float) -> None:
        try:
            await asyncio.sleep(grace_sec)
        except asyncio.CancelledError:
            return
        if self._orch_grace_tasks.get(csm_sid) is not asyncio.current_task():
            return
        self._orch_grace_tasks.pop(csm_sid, None)
        try:
            log.info("mission stage grace elapsed; stopping csm_session=%s", csm_sid)
            await self._session_manager.stop_session(csm_sid, graceful=True)
        except Exception:
            log.exception("orchestrator grace stop_session failed csm_session=%s", csm_sid)

    async def _stamp_stage_run_terminal(
        self,
        run_id: str,
        exit_code: int | None,
        *,
        override_status: RunStatus | None = None,
    ) -> None:
        """Move a mission-stage Run row from RUNNING → terminal.

        Called from `_on_session_ended` after we've decided a stage is
        finished (success advance / validation fail / spec missing).
        Without this stamp the row stays RUNNING and the automation
        runner's `_reap_orphan_runs` watchdog would 60s later flip it to
        FAILED with exit=-1 — even though the stage actually succeeded
        and the mission has already advanced past it. That's the
        "5 stages show FAILED but mission is SUCCEEDED" confusion.

        Idempotent: if the row is already terminal (SUCCEEDED / FAILED /
        NEEDS_REVIEW) we leave it alone — avoids racing the rescuer or
        a synthetic SESSION_ENDED replay from clobbering a legitimate
        terminal state.
        """
        async with self._sm() as db:
            row = await db.get(Run, run_id)
            if row is None:
                return
            if row.status in (
                RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.NEEDS_REVIEW
            ):
                return
            if override_status is not None:
                row.status = override_status
            else:
                row.status = (
                    RunStatus.SUCCEEDED
                    if exit_code in (None, 0)
                    else RunStatus.FAILED
                )
            row.exit_code = exit_code
            row.ended_at = now_utc_naive()
            await db.commit()

    async def _on_session_ended(self, event: Event) -> None:
        """Advance the Mission when a stage's AUTO session terminates.

        Ignores any session that isn't a mission stage (`Run.mission_id IS
        NULL`). For mission stages: runs validation primitives against the
        workflow's compiled rules and decides next state. The decision
        tree mirrors the docstring at the top of the module.

        Subscribers MUST NOT raise per EventStream contract — we catch
        defensively and log.
        """
        try:
            csm_sid = (event.payload or {}).get("csm_session_id")
            if not csm_sid:
                return
            exit_code = (event.payload or {}).get("exit_code")
            # Finding-8: session terminated (from grace-then-stop or PTY EOF);
            # cancel any lingering grace timer + drop the claude→csm mapping
            # so we don't leak state between stages.
            grace = self._orch_grace_tasks.pop(csm_sid, None)
            if grace is not None and not grace.done():
                grace.cancel()
            for cid, sid in list(self._orch_csm_by_external_session.items()):
                if sid == csm_sid:
                    self._orch_csm_by_external_session.pop(cid, None)

            async with self._sm() as db:
                res = await db.execute(
                    select(Run).where(Run.session_id == csm_sid)
                )
                run = res.scalar_one_or_none()
                if run is None:
                    # Fallback for the A3 race: a fast-exiting AUTO child can
                    # emit SESSION_ENDED before `_start_claude_stage` stamps
                    # `Run.session_id`. `Session.associated_run_id` is
                    # written inside `create_session`'s own commit, so it's
                    # authoritative even when the forward link is still NULL.
                    sess_row = await db.get(Session, csm_sid)
                    if sess_row is not None and sess_row.associated_run_id:
                        run = await db.get(Run, sess_row.associated_run_id)
                if run is None or run.mission_id is None:
                    # Not a mission Run — leave to automation runner.
                    return
                mission_id = run.mission_id
                stage_name = run.stage_name
                run_id = run.id

                mission = await db.get(Mission, mission_id)
                if mission is None:
                    log.warning(
                        "session ended for unknown mission %s (run=%s)",
                        mission_id,
                        run.id,
                    )
                    # Session ended cleanly enough for us to have found the
                    # run; the mission row is just gone (pruned?). Reflect
                    # the session termination on the run so it doesn't sit
                    # RUNNING forever.
                    await self._stamp_stage_run_terminal(run_id, exit_code)
                    return
                if mission.status != MissionStatus.RUNNING:
                    # Mission already terminated (cancelled / failed); ignore.
                    log.info(
                        "ignoring session.ended for mission %s in status %s",
                        mission_id,
                        mission.status.value,
                    )
                    # Still update the run so it reflects the terminated
                    # session. If the mission was cancelled mid-stage, the
                    # exit_code will typically be non-zero (SIGTERM=-15 etc).
                    await self._stamp_stage_run_terminal(run_id, exit_code)
                    return

                wf_row = await db.get(WorkflowDefinition, mission.workflow_def_id)
                if wf_row is None:
                    log.warning(
                        "mission %s references missing workflow %s",
                        mission_id,
                        mission.workflow_def_id,
                    )
                    await self._stamp_stage_run_terminal(run_id, exit_code)
                    return
                spec = load_workflow_spec(wf_row.yaml_content)
                compiled = wf_row.compiled_rules or {}
                ws_path = mission.workspace_path
                params = dict(mission.parameters or {})
                # Pull every prior stage's Output rows so cross-stage
                # references (`{stages.X.outputs[N]}`) resolve correctly
                # during BOTH validation and the next stage's prompt.
                prior_outputs = await self._collect_prior_outputs(
                    db, mission_id=mission_id, before_stage=stage_name, spec=spec
                )

            _, stage_spec = _find_stage(spec, stage_name)
            if stage_spec is None:
                log.warning(
                    "mission %s: stage %r not in workflow %s; cannot advance",
                    mission_id,
                    stage_name,
                    spec.name,
                )
                # Stage not declared → mission will fail. Stamp run FAILED
                # so it reflects the terminal decision.
                await self._stamp_stage_run_terminal(
                    run_id, exit_code, override_status=RunStatus.FAILED,
                )
                await self._finalize_mission_failed(
                    mission_id,
                    f"stage {stage_name!r} not declared on workflow {spec.name!r}",
                )
                return

            stage_compiled = (compiled.get("stages") or {}).get(stage_name) or {
                "kind": stage_spec.kind,
                "validation": [],
            }
            report = validate_stage(
                stage_compiled=stage_compiled,
                workspace_path=ws_path,
                params=params,
                prior_outputs=prior_outputs,
            )
            if not report.get("pass", False):
                failed = report.get("failed_checks") or []
                reason = (
                    f"stage {stage_name!r} validation failed: {failed}"
                )
                # Validation fail → mission fails at this stage. Stamp
                # the run FAILED before finalizing so the UI shows the
                # stage that actually broke, not a phantom RUNNING one.
                await self._stamp_stage_run_terminal(
                    run_id, exit_code, override_status=RunStatus.FAILED,
                )
                await self._finalize_mission_failed(mission_id, reason)
                return

            # Validation passed → persist Output rows for each declared
            # output path so downstream stages can reference them via
            # `{stages.X.outputs[N]}`. Skipping this breaks cross-stage
            # data flow — the smoke variant used in T8 had to sidestep
            # this because Output rows never got written.
            await self._persist_stage_outputs(
                run_id=run_id,
                mission_id=mission_id,
                workspace_path=ws_path,
                workflow_name=spec.name,
                params=params,
                prior_outputs=prior_outputs,
                stage_spec=stage_spec,
            )

            # Stamp the finishing stage SUCCEEDED before advancing so
            # the UI reflects it the moment the next stage starts.
            await self._stamp_stage_run_terminal(
                run_id, exit_code, override_status=RunStatus.SUCCEEDED,
            )
            await self._advance_to_next_stage(mission_id, stage_name, spec)
        except Exception:
            log.exception("orchestrator._on_session_ended failed")

    async def _persist_stage_outputs(
        self,
        *,
        run_id: str,
        mission_id: str,
        workspace_path: str,
        workflow_name: str,
        params: dict[str, Any],
        prior_outputs: dict[str, list[str]],
        stage_spec: StageSpec,
    ) -> None:
        """Insert `Output` rows for each entry in `stage_spec.outputs`.

        Each declared output path is rendered through `render_prompt` so
        placeholders (`{ws}` / `{params.X}` / `{stages.Y.outputs[N]}`)
        resolve against the current mission state, then normalised to an
        absolute path (workspace-relative paths get prefixed with the
        mission's workspace_path).

        Behaviour choices:
        - **Non-existent files are still recorded.** Validation just
          passed, so any declared output either exists on disk or was
          explicitly considered optional by the workflow author. Storing
          the path either way keeps `{stages.X.outputs[N]}` resolvable
          for downstream stages even when the file arrives asynchronously.
        - **Render failures are logged, not raised.** A bad placeholder
          here should not roll back the Mission — the stage already
          validated. Downstream renderers will surface the missing entry
          via `PromptRenderError` at the point it matters.
        - Poll stages carry no `outputs` schema field, so this method is
          a no-op for them (called only from the claude branch).
        """
        outputs = stage_spec.outputs or []
        if not outputs:
            return
        resolved: list[str] = []
        for template in outputs:
            try:
                rendered = render_prompt(
                    template,
                    params=params,
                    workspace_path=workspace_path,
                    mission_id=mission_id,
                    workflow_name=workflow_name,
                    stage_outputs=prior_outputs,
                )
            except PromptRenderError as e:
                log.warning(
                    "output path render failed for mission=%s stage=%s path=%r: %s",
                    mission_id,
                    stage_spec.name,
                    template,
                    e,
                )
                continue
            p = Path(rendered)
            if not p.is_absolute():
                p = Path(workspace_path) / p
            resolved.append(str(p))
        if not resolved:
            return
        async with self._sm() as db:
            for path in resolved:
                preview = _safe_preview(path) if Path(path).exists() else None
                db.add(
                    Output(
                        run_id=run_id,
                        path=path,
                        type=_classify_output_type(path),
                        preview=preview,
                    )
                )
            await db.commit()

    async def _advance_to_next_stage(
        self,
        mission_id: str,
        completed_stage: str,
        spec: WorkflowSpec,
    ) -> None:
        """Move the Mission from `completed_stage` to whatever comes next.

        Called from two places:
        - `_on_session_ended` after a claude stage's validation passes
        - `_on_poll_pass` after a poll stage's check converges

        If `completed_stage` was the last stage in the workflow → mission
        is finalised as succeeded. Otherwise the Mission row's
        `current_stage` is updated, STATE.yaml is rewritten, and the next
        stage is launched (claude → spawn AUTO session; poll → start
        poll loop).
        """
        stage_idx, _ = _find_stage(spec, completed_stage)
        if stage_idx < 0:
            log.warning(
                "_advance_to_next_stage: stage %r not in workflow %s",
                completed_stage,
                spec.name,
            )
            await self._finalize_mission_failed(
                mission_id,
                f"stage {completed_stage!r} not declared on workflow {spec.name!r}",
            )
            return

        next_idx = stage_idx + 1
        if next_idx >= len(spec.stages):
            await self._finalize_mission_succeeded(mission_id)
            return

        next_stage = spec.stages[next_idx]
        async with self._sm() as db:
            m = await db.get(Mission, mission_id)
            if m is None or m.status != MissionStatus.RUNNING:
                return
            m.current_stage = next_stage.name
            m.audit_log = (m.audit_log or []) + [
                {
                    "ts": _iso_now(),
                    "event": "stage_advanced",
                    "from": completed_stage,
                    "to": next_stage.name,
                }
            ]
            await db.commit()
            await db.refresh(m)

        # Snapshot the new current_stage BEFORE we hand off to the next
        # stage's launcher; the launcher rewrites STATE.yaml again after
        # it inserts its Run row.
        await self._write_state_yaml(mission_id)
        if next_stage.kind == "poll":
            await self._poll_executor.start_poll(
                mission_id, next_stage.name, next_stage
            )
        else:
            await self._start_claude_stage(m, next_stage)

    # ------------------------------------------------------------------
    # Poll executor callbacks
    # ------------------------------------------------------------------

    async def _on_poll_pass(self, mission_id: str, stage_name: str) -> None:
        """PollExecutor calls this when a poll stage's check converges."""
        try:
            async with self._sm() as db:
                mission = await db.get(Mission, mission_id)
                if mission is None or mission.status != MissionStatus.RUNNING:
                    return
                wf_row = await db.get(WorkflowDefinition, mission.workflow_def_id)
                if wf_row is None:
                    return
                spec = load_workflow_spec(wf_row.yaml_content)
            await self._advance_to_next_stage(mission_id, stage_name, spec)
        except Exception:
            log.exception(
                "orchestrator._on_poll_pass failed mission=%s stage=%s",
                mission_id,
                stage_name,
            )

    async def _on_poll_fail(
        self, mission_id: str, stage_name: str, reason: str
    ) -> None:
        """PollExecutor calls this when max_attempts is exhausted."""
        try:
            await self._finalize_mission_failed(mission_id, reason)
        except Exception:
            log.exception(
                "orchestrator._on_poll_fail failed mission=%s stage=%s",
                mission_id,
                stage_name,
            )

    # ------------------------------------------------------------------
    # T6 cron rescuer
    # ------------------------------------------------------------------

    async def _rescuer_loop(self) -> None:
        """Delegator — real body lives in `orchestrator_reaper.rescuer_loop`."""
        await _reaper.rescuer_loop(self)

    async def _rescue_pass(self) -> None:
        """Delegator — real body lives in `orchestrator_reaper.rescue_pass`."""
        await _reaper.rescue_pass(self)

    @staticmethod
    async def _mission_timeout_sec(
        db: Any,
        mission: Mission,
        spec_cache: dict[str, WorkflowSpec | None],
    ) -> int | None:
        """Resolve the mission's effective wall-clock budget in seconds.

        Reads `global_timeout` off the cached WorkflowSpec (PRD §3, default
        604800s == 7 days). Returns None if the workflow row is missing or
        the YAML fails to parse — both treated as "no timeout enforced",
        because the alternative (fail the mission on every rescuer pass)
        would be far more destructive than letting the audit log carry the
        operator-visible warning.
        """
        wf_id = mission.workflow_def_id
        if wf_id in spec_cache:
            spec = spec_cache[wf_id]
        else:
            wf_row = await db.get(WorkflowDefinition, wf_id)
            if wf_row is None:
                spec_cache[wf_id] = None
                return None
            try:
                spec = load_workflow_spec(wf_row.yaml_content)
            except Exception:
                log.exception(
                    "rescuer: failed to parse workflow spec for mission=%s",
                    mission.id,
                )
                spec_cache[wf_id] = None
                return None
            spec_cache[wf_id] = spec
        if spec is None:
            return None
        return spec.global_timeout

    @staticmethod
    def _stage_kind_from_spec(
        mission: Mission,
        stage_name: str,
        spec_cache: dict[str, WorkflowSpec | None],
    ) -> str | None:
        """Look up a stage's `kind` via the spec_cache warmed by `_mission_timeout_sec`.

        Returns "claude" / "poll" if resolvable, else None. `_rescue_pass`
        uses this to skip poll-kind stages before the Run-row check —
        poll stages are driven by PollExecutor and never produce Run
        rows, so the pre-fix code would finalize FAILED on the first
        tick after a mission advanced into wait_train / wait_eval.

        Fail-safe on unresolvable cases (missing workflow row, YAML
        parse failure, stale stage name not in spec) so the rescuer
        falls back to its pre-fix behaviour rather than silently
        skipping genuinely-broken missions.
        """
        spec = spec_cache.get(mission.workflow_def_id)
        if spec is None:
            return None
        for s in spec.stages:
            if s.name == stage_name:
                return s.kind
        return None

    async def _dispatch_timeout(
        self,
        mission_id: str,
        timeout_sec: int,
        session_id: str | None,
        stage_name: str | None = None,
        stage_kind: str | None = None,
    ) -> None:
        """Finalise a timed-out mission, best-effort cancelling its work.

        Order matters: poll loops are cancelled first so they can't fire
        `on_pass` after the mission has been finalised; then `stop_session`
        targets any live claude PID; then `_finalize_mission_failed` flips
        the row to FAILED and rewrites STATE.yaml.

        `stage_name` / `stage_kind` are threaded through only to annotate
        the failure reason so post-mortems can distinguish a poll-stage
        wall-clock kill (global budget hit while a per-stage poll timeout
        still had headroom) from a claude-stage timeout.
        """
        try:
            await self._poll_executor.cancel_all_for_mission(mission_id)
        except Exception:
            log.exception(
                "rescuer: cancel_all_for_mission failed during timeout mission=%s",
                mission_id,
            )

        if session_id:
            try:
                pid = await self._lookup_session_pid(session_id)
                if pid is not None and self._is_pid_alive(pid):
                    await self._session_manager.stop_session(session_id)
            except Exception:
                log.exception(
                    "rescuer: stop_session failed during timeout mission=%s session=%s",
                    mission_id,
                    session_id,
                )

        reason = f"mission timed out at {timeout_sec}s"
        if stage_kind == "poll" and stage_name:
            reason += (
                f" (current stage {stage_name!r} is poll-kind — global "
                f"budget hit before the per-stage timeout could expire)"
            )
        await self._finalize_mission_failed(mission_id, reason)

    async def _startup_reap(self) -> None:
        """Delegator — real body lives in `orchestrator_reaper.startup_reap`."""
        await _reaper.startup_reap(self)

    async def _dispatch_rescue_decision(
        self,
        mission_id: str,
        stage_name: str | None,
        run: Run | None,
        workflow_def_id: str | None = None,
    ) -> None:
        """Apply the rescue decision table for one mission.

        Re-checks `Mission.status == RUNNING` inside the actioning paths
        (`_finalize_mission_failed` / `_on_session_ended`) so concurrent
        progress made between `_rescue_pass`'s scan and this dispatch
        doesn't race the rescuer into overriding fresh state.

        `workflow_def_id` is threaded through only to annotate the
        "no run for stage" failure — when a mission's workflow row is
        deleted or its YAML becomes unparseable mid-run, the caller
        cannot classify the stage as poll and falls through to this
        finalize; the audit log should point to the root cause.
        """
        if not stage_name:
            await self._finalize_mission_failed(
                mission_id,
                "rescuer: mission has no current_stage",
            )
            return

        if run is None:
            reason = f"rescuer: no run for current stage {stage_name!r}"
            if workflow_def_id is not None:
                reason += (
                    f" (workflow_def_id={workflow_def_id!r}; if this stage "
                    f"was expected to be poll-kind, the workflow spec may "
                    f"be missing or unparseable)"
                )
            await self._finalize_mission_failed(mission_id, reason)
            return

        if run.status == RunStatus.SUCCEEDED:
            # The Run already finished cleanly but mission stayed RUNNING —
            # SESSION_ENDED must have been lost. Dispatch a synthetic event
            # so the standard advancement/validation pipeline runs exactly
            # the same as the real path would have. Idempotent: if the
            # mission has already advanced, _on_session_ended's stage-name
            # lookup just no-ops.
            if not run.session_id:
                await self._finalize_mission_failed(
                    mission_id,
                    f"rescuer: stage {stage_name!r} succeeded run "
                    f"{run.id!r} has no session_id; cannot dispatch synthetic SESSION_ENDED",
                )
                return
            synth = Event(
                type=EventType.SESSION_ENDED,
                ts=now_utc_naive(),
                session_id=None,
                project_path=None,
                payload={
                    "csm_session_id": run.session_id,
                    "exit_code": run.exit_code if run.exit_code is not None else 0,
                    "source": "workflow-rescuer",
                },
            )
            log.info(
                "rescuer: dispatching synthetic SESSION_ENDED mission=%s stage=%s run=%s",
                mission_id,
                stage_name,
                run.id,
            )
            await self._on_session_ended(synth)
            return

        if run.status == RunStatus.FAILED:
            reason = (
                f"rescuer: stage {stage_name!r} run {run.id!r} reported "
                f"FAILED (exit_code={run.exit_code}); SESSION_ENDED appears lost"
            )
            await self._finalize_mission_failed(mission_id, reason)
            return

        # Run is RUNNING (or PENDING / NEEDS_REVIEW which we treat as live).
        # Probe the OS PID to decide if the process is actually alive.
        if not run.session_id:
            # No session yet — could be a transient race between Run insert
            # and session_id stamping in `_start_claude_stage`. Leave it.
            return

        pid = await self._lookup_session_pid(run.session_id)
        if pid is None:
            return  # session gone or no PID yet — leave alone for next tick

        if not self._is_pid_alive(pid):
            # Finding-4: same grace window that startup_reap uses. Cover the
            # case where the rescuer fires within the first tick after a CSM
            # restart, before startup_reap's own grace-skip is enough to hide
            # the pid-dead reading.
            if await self._session_within_reap_grace(run.session_id):
                log.info(
                    "rescuer: orphan-PID grace-skip mission=%s session=%s pid=%s "
                    "(within %.0fs of last activity)",
                    mission_id,
                    run.session_id,
                    pid,
                    self._REAP_GRACE_SEC,
                )
                return
            await self._finalize_mission_failed(
                mission_id,
                f"rescuer: orphaned stage run pid={pid} "
                f"(mission stage={stage_name!r}, run={run.id!r})",
            )

    async def _lookup_session_pid(self, session_id: str) -> int | None:
        async with self._sm() as db:
            sess = await db.get(Session, session_id)
            if sess is None:
                return None
            return sess.pid

    async def _session_within_reap_grace(self, session_id: str) -> bool:
        """Finding-4: return True if the session's last recorded activity is
        recent enough that a pid-dead reading is likely a CSM restart in
        progress rather than a real orphan. Rescuer callers should skip
        reaping and let the next tick re-evaluate."""
        async with self._sm() as db:
            sess = await db.get(Session, session_id)
            if sess is None:
                return False
            marker = sess.last_activity_ts or sess.started_at
            if marker is None:
                return False
            age = (now_utc_naive() - marker).total_seconds()
            return age < self._REAP_GRACE_SEC

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """`os.kill(pid, 0)` — signal 0 just probes for existence/permission.

        `ProcessLookupError` is the orphan signal. `PermissionError` means
        the PID exists but belongs to another user; we treat that as alive
        because the rescuer's goal is not to mark a healthy-but-foreign
        process as orphaned. Anything else (e.g. OSError) is conservatively
        treated as alive so we don't fail a mission on a transient errno.
        """
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True

    async def _finalize_mission_succeeded(self, mission_id: str) -> None:
        """Delegator — real body lives in `orchestrator_reaper.finalize_mission_succeeded`."""
        await _reaper.finalize_mission_succeeded(self, mission_id)

    async def _finalize_mission_failed(self, mission_id: str, reason: str) -> None:
        """Delegator — real body lives in `orchestrator_reaper.finalize_mission_failed`."""
        await _reaper.finalize_mission_failed(self, mission_id, reason)

    async def _emit_mission_ended(
        self,
        mission_id: str,
        status: str,
        workflow_name: str | None,
        failure_reason: str | None,
    ) -> None:
        """Fan out a MISSION_ENDED event so NotificationBus can raise a bell.

        Best-effort — an emit failure must not undo the state transition.
        """
        try:
            await self._es.emit(Event(
                type=EventType.MISSION_ENDED,
                ts=now_utc_naive(),
                session_id=None,
                project_path=None,
                payload={
                    "mission_id": mission_id,
                    "status": status,
                    "workflow_name": workflow_name,
                    "failure_reason": failure_reason,
                },
            ))
        except Exception:
            log.exception("failed to emit MISSION_ENDED for %s", mission_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_params(spec: WorkflowSpec, params: dict[str, Any]) -> None:
        """Ensure every required parameter has a value (no type coercion yet).

        Type coercion / default population are deferred to T4 where prompt
        rendering needs canonical typed values. T2 only blocks launches
        that omit a required field — that's enough to keep callers honest.
        """
        missing: list[str] = []
        for p in spec.parameters:
            if not p.required:
                continue
            if p.name not in params:
                if p.default is None:
                    missing.append(p.name)
        if missing:
            raise ValueError(
                f"workflow {spec.name!r}: missing required parameters: {missing}"
            )

    @staticmethod
    def _resolve_workspace(template: str, mission_id: str) -> str:
        """Substitute `{mission_id}` and resolve relative paths under project root."""
        rendered = template.replace("{mission_id}", mission_id)
        p = Path(rendered)
        if not p.is_absolute():
            p = settings.project_root / p
        return str(p)

    @staticmethod
    def _check_transition(mission: Mission, to: MissionStatus) -> None:
        allowed = _VALID_TRANSITIONS.get(mission.status, set())
        if to not in allowed:
            raise InvalidMissionStateTransition(
                f"mission {mission.id!r}: illegal transition "
                f"{mission.status.value} → {to.value} "
                f"(allowed from {mission.status.value}: "
                f"{sorted(s.value for s in allowed)})"
            )

    # ------------------------------------------------------------------
    # Prior-stage outputs (T4)
    # ------------------------------------------------------------------

    @staticmethod
    async def _collect_prior_outputs(
        db: Any,
        *,
        mission_id: str,
        before_stage: str,
        spec: WorkflowSpec,
    ) -> dict[str, list[str]]:
        """Collect each prior stage's discovered Output paths.

        Returns a mapping `{stage_name: [path, path, ...]}` covering every
        stage that appears **before** `before_stage` in `spec.stages`. The
        ordering inside each list is the order the Output rows were
        discovered (the runner inserts them in spec-glob order, so this
        matches the YAML output index).

        Stages that have no Output rows (e.g. a stage whose run is still
        in flight or whose outputs haven't been discovered yet) are
        omitted entirely — the caller's renderer will then raise
        `PromptRenderError` if the template actually references those
        outputs, which is the right failure mode.
        """
        prior_names: list[str] = []
        for s in spec.stages:
            if s.name == before_stage:
                break
            prior_names.append(s.name)
        if not prior_names:
            return {}

        rows = (
            await db.execute(
                select(Run.stage_name, Output.path)
                .join(Output, Output.run_id == Run.id)
                .where(
                    Run.mission_id == mission_id,
                    Run.stage_name.in_(prior_names),
                )
                .order_by(Output.discovered_at)
            )
        ).all()
        out: dict[str, list[str]] = {}
        for stage_name, path in rows:
            out.setdefault(stage_name, []).append(path)
        return out

    # ------------------------------------------------------------------
    # STATE.yaml snapshot (T4)
    # ------------------------------------------------------------------

    async def _resolve_user_default_agent(self) -> str:
        """Multi-agent v2 helper — look up UserPreference.default_agent.

        Falls back to 'claude' on any DB error so a busted preferences
        row can't wedge the whole workflow subsystem.
        """
        try:
            from csm.models import UserPreference
            async with self._sm() as db:
                pref = await db.get(UserPreference, 1)
                if pref is not None:
                    return pref.default_agent
        except Exception:
            log.exception("workflow: user preference read failed; defaulting to claude")
        return "claude"

    async def _write_state_yaml(self, mission_id: str) -> None:
        """Thin delegator — see `orchestrator_state.write_state_yaml`."""
        await _state_yaml.write_state_yaml(self, mission_id)

    async def _build_state_snapshot(
        self, mission_id: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Thin delegator — see `orchestrator_state.build_state_snapshot`."""
        return await _state_yaml.build_state_snapshot(self, mission_id)

    @staticmethod
    def _render_static_outputs(
        templates: list[str],
        *,
        mission: Mission,
        workflow_name: str,
    ) -> list[str]:
        """Substitute the cheap placeholders into an output template list.

        Used for STATE.yaml's "what was declared" view of a stage that
        hasn't produced Output rows yet. Cross-stage references are left
        as-is — the snapshot is informational, not a live prompt, and a
        future stage's outputs may legitimately reference upstream paths
        that exist in the snapshot just below it.
        """
        rendered: list[str] = []
        params = dict(mission.parameters or {})
        for tpl in templates:
            text = tpl
            text = text.replace("{ws}", mission.workspace_path)
            text = text.replace("{mission_id}", mission.id)
            text = text.replace("{workflow_name}", workflow_name)
            for k, v in params.items():
                text = text.replace(f"{{params.{k}}}", str(v))
            rendered.append(text)
        return rendered


def _iso_now() -> str:
    return now_utc_naive().isoformat(timespec="seconds") + "Z"


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat(timespec="seconds") + ("Z" if dt.tzinfo is None else "")


def _find_stage(
    spec: WorkflowSpec, name: str | None
) -> tuple[int, StageSpec | None]:
    if name is None:
        return (-1, None)
    for i, s in enumerate(spec.stages):
        if s.name == name:
            return (i, s)
    return (-1, None)

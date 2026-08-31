"""Split-out from orchestrator.py: rescue loop + startup reap + finalize helpers.

Kept as module-level async fns that accept an orchestrator instance so the
split is a pure code move, not a class-splitting refactor. The
`WorkflowOrchestrator` retains thin delegator methods (`_rescuer_loop`,
`_startup_reap`, `_finalize_mission_failed`, `_finalize_mission_succeeded`)
that forward here — every existing caller (production or tests) that says
`await self._foo(...)` / `await orch._foo(...)` continues to work, and any
`monkeypatch.setattr(orch, "_foo", spy)` in tests still shadows the
delegator via normal instance-attribute lookup.

Internal cross-calls that used to be `self._other(...)` are rewritten to
`orch._other(...)` — same semantics, same monkeypatch-friendliness.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from csm.core.events import Event, EventType
from csm.models import (
    Mission,
    MissionStatus,
    Run,
    WorkflowDefinition,
)
from csm.models.run import RunStatus
from csm.modules.workflow.schema import WorkflowSpec
from csm.utils.time import now_utc_naive

if TYPE_CHECKING:
    from csm.modules.workflow.orchestrator import WorkflowOrchestrator

log = logging.getLogger(__name__)


def _iso_now() -> str:
    return now_utc_naive().isoformat(timespec="seconds") + "Z"


async def rescuer_loop(orch: WorkflowOrchestrator) -> None:
    """Periodically reconcile RUNNING missions against live process state.

    Guards against the edge case where a stage's AUTO session dies
    without emitting `SESSION_ENDED` — e.g. SIGKILL while the JSONL
    tail was between ticks, or the session manager's reaper raced the
    event-stream subscriber. Without this loop the Mission row would
    hang in RUNNING forever, and a poll-kind successor would never
    be scheduled.
    """
    assert orch._stopping is not None, "start() must run before _rescuer_loop"
    # First pass runs immediately after start so a backend restart
    # surface mission state within ~one scan tick of being healthy
    # rather than 30s later. Subsequent passes follow the cadence.
    while not orch._stopping.is_set():
        try:
            await orch._rescue_pass()
        except Exception:
            log.exception("rescuer pass failed")
        try:
            await asyncio.wait_for(
                orch._stopping.wait(), timeout=orch._RESCUE_INTERVAL_SEC
            )
        except TimeoutError:
            continue


async def rescue_pass(orch: WorkflowOrchestrator) -> None:
    """One sweep across all RUNNING missions.

    Two-phase dispatch:
    1. **Timeout check (T7)** — if `mission.started_at + global_timeout
       < now`, short-circuit the run-state branch and queue a timeout
       dispatch. The mission's session (if alive) gets SIGTERM'd via
       `session_manager.stop_session` and any owned poll loop is
       cancelled before `_finalize_mission_failed` runs.
    2. **Run-state branch (T6)** — for non-timed-out missions, look
       up the latest Run for `current_stage` and queue a regular
       rescue decision.

    Defensive at every layer: top-level try/except so a transient DB
    error doesn't crash the loop, per-mission try/except so a single
    sick mission doesn't poison its neighbours.
    """
    try:
        async with orch._sm() as db:
            missions = (
                await db.execute(
                    select(Mission).where(
                        Mission.status == MissionStatus.RUNNING
                    )
                )
            ).scalars().all()
            now = now_utc_naive()
            # (mission_id, timeout_sec, session_id, stage_name, stage_kind)
            timeout_decisions: list[
                tuple[str, int, str | None, str | None, str | None]
            ] = []
            # (mission_id, stage_name, run_or_None, workflow_def_id)
            run_decisions: list[
                tuple[str, str | None, Run | None, str | None]
            ] = []
            # Cache parsed specs within a single pass — missions that
            # share a workflow only pay the YAML parse once.
            spec_cache: dict[str, WorkflowSpec | None] = {}

            for mission in missions:
                timeout_sec = await orch._mission_timeout_sec(
                    db, mission, spec_cache
                )
                if (
                    timeout_sec is not None
                    and mission.started_at is not None
                    and (now - mission.started_at).total_seconds() > timeout_sec
                ):
                    session_id_to_stop: str | None = None
                    if mission.current_stage:
                        tmo_run = await db.scalar(
                            select(Run)
                            .where(
                                Run.mission_id == mission.id,
                                Run.stage_name == mission.current_stage,
                            )
                            .order_by(Run.started_at.desc())
                            .limit(1)
                        )
                        if tmo_run is not None:
                            session_id_to_stop = tmo_run.session_id
                    tmo_stage_kind = (
                        orch._stage_kind_from_spec(
                            mission, mission.current_stage, spec_cache
                        )
                        if mission.current_stage
                        else None
                    )
                    timeout_decisions.append(
                        (
                            mission.id,
                            timeout_sec,
                            session_id_to_stop,
                            mission.current_stage,
                            tmo_stage_kind,
                        )
                    )
                    continue

                stage_name = mission.current_stage
                run = None
                if stage_name:
                    if orch._stage_kind_from_spec(
                        mission, stage_name, spec_cache
                    ) == "poll":
                        continue
                    run = await db.scalar(
                        select(Run)
                        .where(
                            Run.mission_id == mission.id,
                            Run.stage_name == stage_name,
                        )
                        .order_by(Run.started_at.desc())
                        .limit(1)
                    )
                run_decisions.append(
                    (mission.id, stage_name, run, mission.workflow_def_id)
                )
    except Exception:
        log.exception("rescuer: top-level select failed")
        return

    # Act on decisions outside the read-only session so each dispatch
    # path (finalize_failed / _on_session_ended) owns its own write txn
    # cleanly and per-mission failures don't roll back the whole pass.
    for mission_id, timeout_sec, session_id, stage_name, stage_kind in timeout_decisions:
        try:
            await orch._dispatch_timeout(
                mission_id, timeout_sec, session_id, stage_name, stage_kind
            )
        except Exception:
            log.exception(
                "rescuer: timeout dispatch failed mission=%s",
                mission_id,
            )
    for mission_id, stage_name, run, wf_def_id in run_decisions:
        try:
            await orch._dispatch_rescue_decision(
                mission_id, stage_name, run, wf_def_id
            )
        except Exception:
            log.exception(
                "rescuer: dispatch failed mission=%s stage=%s",
                mission_id,
                stage_name,
            )


async def startup_reap(orch: WorkflowOrchestrator) -> None:
    """One-shot orphan-PID reap, called once from `start()`.

    Strict subset of `_rescue_pass`'s orphan-PID branch: scan every
    RUNNING mission whose current stage has a RUNNING Run with a
    session_id whose OS PID is no longer alive, and finalise it
    FAILED with reason `"reaped on startup"`. Deliberately does NOT:
    - synthesize SESSION_ENDED for SUCCEEDED runs (those wait for the
      first periodic pass — the audit log is clearer that way);
    - fail missions whose Run row is missing entirely;
    - timeout-check (the periodic loop will get to it within 30s).

    Top-level try/except wraps the DB scan so a broken sessionmaker
    (or an empty session_manager after a crash that lost the in-memory
    `_live` table) does not crash `start()`.
    """
    candidates: list[tuple[str, str]] = []  # (mission_id, session_id)
    try:
        async with orch._sm() as db:
            missions = (
                await db.execute(
                    select(Mission).where(
                        Mission.status == MissionStatus.RUNNING
                    )
                )
            ).scalars().all()
            for mission in missions:
                if not mission.current_stage:
                    continue
                run = await db.scalar(
                    select(Run)
                    .where(
                        Run.mission_id == mission.id,
                        Run.stage_name == mission.current_stage,
                    )
                    .order_by(Run.started_at.desc())
                    .limit(1)
                )
                if run is None or run.status != RunStatus.RUNNING:
                    # Poll-stage missions have no Run rows; the None check
                    # is also their safe skip path. Do not tighten this
                    # without also gating on stage_kind (see _rescue_pass).
                    continue
                if not run.session_id:
                    continue
                candidates.append((mission.id, run.session_id))
    except Exception:
        log.exception("rescuer: _startup_reap top-level select failed")
        return

    for mission_id, session_id in candidates:
        try:
            pid = await orch._lookup_session_pid(session_id)
            if pid is None:
                continue
            if orch._is_pid_alive(pid):
                continue
            # Finding-4: give a short CSM restart time to breathe before
            # declaring a pid-dead session an orphan. The periodic rescuer
            # will re-evaluate on the next tick, and the grace expires
            # naturally without any state change on our part.
            if await orch._session_within_reap_grace(session_id):
                log.info(
                    "rescuer: _startup_reap grace-skip mission=%s session=%s pid=%s "
                    "(within %.0fs of last activity)",
                    mission_id,
                    session_id,
                    pid,
                    orch._REAP_GRACE_SEC,
                )
                continue
            await orch._finalize_mission_failed(
                mission_id,
                f"reaped on startup (pid={pid} not alive)",
            )
        except Exception:
            log.exception(
                "rescuer: _startup_reap dispatch failed mission=%s session=%s",
                mission_id,
                session_id,
            )


async def finalize_mission_succeeded(
    orch: WorkflowOrchestrator, mission_id: str
) -> None:
    async with orch._sm() as db:
        mission = await db.get(Mission, mission_id)
        if mission is None:
            return
        orch._check_transition(mission, MissionStatus.SUCCEEDED)
        prev = mission.status
        mission.status = MissionStatus.SUCCEEDED
        mission.ended_at = now_utc_naive()
        mission.audit_log = (mission.audit_log or []) + [
            {
                "ts": _iso_now(),
                "event": "succeeded",
                "from": prev.value,
                "to": MissionStatus.SUCCEEDED.value,
            }
        ]
        # Preserve the cross-table invariant even when an earlier event path
        # advanced the mission before stamping its final stage execution.
        runs = (
            await db.execute(select(Run).where(Run.mission_id == mission_id))
        ).scalars().all()
        for run in runs:
            if run.status in (RunStatus.PENDING, RunStatus.RUNNING):
                run.status = RunStatus.SUCCEEDED
                run.ended_at = mission.ended_at
        wf_id = mission.workflow_def_id
        wf_name = None
        if wf_id:
            wf = await db.get(WorkflowDefinition, wf_id)
            if wf is not None:
                wf_name = wf.name
        await db.commit()
    await orch._write_state_yaml(mission_id)
    log.info("mission %s succeeded", mission_id)
    await orch._emit_mission_ended(mission_id, "succeeded", wf_name, None)


async def finalize_mission_failed(
    orch: WorkflowOrchestrator, mission_id: str, reason: str
) -> None:
    async with orch._sm() as db:
        mission = await db.get(Mission, mission_id)
        if mission is None:
            return
        orch._check_transition(mission, MissionStatus.FAILED)
        prev = mission.status
        mission.status = MissionStatus.FAILED
        mission.failure_reason = reason
        mission.ended_at = now_utc_naive()
        mission.audit_log = (mission.audit_log or []) + [
            {
                "ts": _iso_now(),
                "event": "failed",
                "from": prev.value,
                "to": MissionStatus.FAILED.value,
                "reason": reason,
            }
        ]
        # A terminal mission must not retain apparently-live stages. The
        # periodic rescuer scans RUNNING missions only, so failing to stamp
        # these here would make the inconsistency permanent.
        runs = (
            await db.execute(select(Run).where(Run.mission_id == mission_id))
        ).scalars().all()
        for run in runs:
            if run.status in (RunStatus.PENDING, RunStatus.RUNNING):
                run.status = RunStatus.FAILED
                run.ended_at = mission.ended_at
        wf_id = mission.workflow_def_id
        wf_name = None
        if wf_id:
            wf = await db.get(WorkflowDefinition, wf_id)
            if wf is not None:
                wf_name = wf.name
        await db.commit()
    await orch._write_state_yaml(mission_id)
    log.info("mission %s failed: %s", mission_id, reason)
    await orch._emit_mission_ended(mission_id, "failed", wf_name, reason)


# Re-exported so any callers reaching for `orchestrator_reaper.Event` /
# `.EventType` for synthetic dispatch have a stable surface. Not used by
# the extracted fns themselves right now, but the module-level imports
# above keep them available.
__all__ = [
    "rescuer_loop",
    "rescue_pass",
    "startup_reap",
    "finalize_mission_succeeded",
    "finalize_mission_failed",
    "Event",
    "EventType",
]

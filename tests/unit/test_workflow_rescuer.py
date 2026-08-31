"""Tests for WorkflowOrchestrator's T6 cron rescuer.

The rescuer is the safety net for the (rare) case where a stage's AUTO
session dies without emitting `SESSION_ENDED` — `stop_session` failed AND
the JSONL tail missed the assistant_done. Without it, the Mission row
would sit in RUNNING forever.

Decision table (also in `orchestrator.py`):

| Run state                      | Action                           |
|--------------------------------|----------------------------------|
| missing                        | fail Mission                     |
| RunStatus.SUCCEEDED            | dispatch synthetic SESSION_ENDED |
|                                | (validation + advance)           |
| RunStatus.FAILED               | fail Mission                     |
| RunStatus.RUNNING + PID alive  | leave alone                      |
| RunStatus.RUNNING + PID dead   | fail Mission                     |

These tests cover every row.

Note on PID mocking: we patch `os.kill` on the orchestrator module rather
than really fork+kill a child. The brief explicitly says "不要真起进程".
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from csm.models import (
    Base,
    Mission,
    MissionStatus,
    Run,
    Session,
    WorkflowDefinition,
    WorkflowReviewStatus,
)
from csm.models.run import RunStatus
from csm.models.session import SessionStatus, SessionType
from csm.modules.workflow.engine import compile_workflow
from csm.modules.workflow.orchestrator import WorkflowOrchestrator
from csm.modules.workflow.schema import load_workflow_spec
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# ----------------------------------------------------------------------------
# Fixtures (mirrors test_workflow_orchestrator.py style)
# ----------------------------------------------------------------------------


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()
    os.unlink(path)


def _fake_session_manager() -> AsyncMock:
    mgr = AsyncMock()
    sess = MagicMock()
    sess.id = "csm-sess-fake"
    sess.external_session_id = "claude-sess-fake"
    mgr.create_session = AsyncMock(return_value=sess)
    return mgr


def _make_orch(sm, *, session_manager=None) -> WorkflowOrchestrator:
    return WorkflowOrchestrator(
        sessionmaker=sm,
        event_stream=MagicMock(),
        session_manager=session_manager if session_manager is not None else _fake_session_manager(),
        runner=MagicMock(),
        workflow_loader=MagicMock(),
    )


# Two stages, no validation block — keeps the SUCCEEDED advancement path
# from depending on file-existence primitives in the rescue tests.
_TWO_STAGE_YAML = """\
name: rescuer_two_stage
description: T6 rescuer fixture
parameters:
  - name: topic
    type: string
    required: true
stages:
  - name: design
    kind: claude
    prompt: "Design"
    outputs:
      - "{ws}/design.md"
  - name: review
    kind: claude
    prompt: "Review {stages.design.outputs[0]}"
    outputs:
      - "{ws}/review.md"
"""


# Two-stage workflow with a poll tail — mirrors real_sample_pipeline's
# `experiment_launch → wait_train` shape. Used by the poll-skip tests.
_POLL_STAGE_YAML = """\
name: rescuer_poll_stage
description: T6 rescuer fixture (with poll stage)
parameters:
  - name: topic
    type: string
    required: true
stages:
  - name: launch
    kind: claude
    prompt: "Launch"
    outputs:
      - "{ws}/launch.done"
  - name: wait_train
    kind: poll
    poll_interval: 60s
    timeout: 3600s
    check:
      - file: "{ws}/train.done"
        primitives:
          - file_exists
"""


async def _seed_workflow(
    sm,
    *,
    name: str = "rescuer_two_stage",
    yaml_content: str = _TWO_STAGE_YAML,
) -> WorkflowDefinition:
    spec = load_workflow_spec(yaml_content)
    compiled = compile_workflow(spec)
    async with sm() as s:
        wf = WorkflowDefinition(
            name=spec.name,
            description=spec.description,
            file_path=f"/tmp/{spec.name}.workflow.yaml",
            yaml_content=yaml_content,
            compiled_rules=compiled,
            review_status=WorkflowReviewStatus.PASSED,
        )
        s.add(wf)
        await s.commit()
        await s.refresh(wf)
        return wf


async def _seed_mission(
    sm,
    wf: WorkflowDefinition,
    *,
    workspace_path: str,
    current_stage: str = "design",
    parameters: dict[str, Any] | None = None,
) -> Mission:
    async with sm() as s:
        m = Mission(
            workflow_def_id=wf.id,
            parameters=parameters or {"topic": "x"},
            workspace_path=workspace_path,
            status=MissionStatus.RUNNING,
            current_stage=current_stage,
        )
        s.add(m)
        await s.commit()
        await s.refresh(m)
        return m


async def _seed_run(
    sm,
    mission: Mission,
    *,
    stage_name: str,
    session_id: str | None,
    status: RunStatus,
    exit_code: int | None = None,
) -> Run:
    async with sm() as s:
        run = Run(  # mission-owned Run (no TaskDef binding)
            session_id=session_id,
            status=status,
            parameters=dict(mission.parameters or {}),
            mission_id=mission.id,
            stage_name=stage_name,
            exit_code=exit_code,
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return run


async def _seed_session(
    sm,
    *,
    session_id: str,
    pid: int | None,
    cwd: str,
    activity_age_sec: int | None = None,
) -> Session:
    """Insert a Session row.

    `activity_age_sec` backdates `started_at` and `last_activity_ts` — needed
    for orphan-reap tests since Finding-4 introduced a 60s grace window: a
    session whose last activity is fresher than the window survives the reap.
    Passing e.g. 600 makes the row appear stale enough to trigger reap.
    """
    async with sm() as s:
        kwargs: dict[str, Any] = {
            "id": session_id,
            "title": "rescuer-fixture",
            "type": SessionType.AUTO,
            "cwd": cwd,
            "status": SessionStatus.RUNNING,
            "pid": pid,
        }
        if activity_age_sec is not None:
            old = datetime.utcnow() - timedelta(seconds=activity_age_sec)
            kwargs["started_at"] = old
            kwargs["last_activity_ts"] = old
        sess = Session(**kwargs)
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        return sess


# ----------------------------------------------------------------------------
# Decision-table tests
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rescue_orphan_pid_marks_mission_failed(db, tmp_path, monkeypatch):
    """RUNNING mission + RUNNING run + dead PID → mission FAILED('orphaned')."""
    from csm.modules.workflow import orchestrator as orch_mod

    wf = await _seed_workflow(db)
    ws = tmp_path / "ws-orphan"
    ws.mkdir()
    mission = await _seed_mission(db, wf, workspace_path=str(ws))
    # Finding-4: reap has a 60s grace window; backdate so this test still
    # exercises the orphan branch instead of grace-skipping.
    await _seed_session(db, session_id="sess-orphan", pid=424242, cwd=str(ws), activity_age_sec=600)
    await _seed_run(
        db,
        mission,
        stage_name="design",
        session_id="sess-orphan",
        status=RunStatus.RUNNING,
    )

    orch = _make_orch(db)

    def _kill_raises(pid, sig):
        raise ProcessLookupError(f"no process {pid}")

    monkeypatch.setattr(orch_mod.os, "kill", _kill_raises)

    await orch._rescue_pass()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.FAILED
        assert m.ended_at is not None
        assert m.failure_reason and "orphaned stage run pid=424242" in m.failure_reason
        assert m.audit_log[-1]["event"] == "failed"
        assert "orphaned" in m.audit_log[-1]["reason"]


@pytest.mark.asyncio
async def test_rescue_orphan_pid_grace_skips_recent_activity(db, tmp_path, monkeypatch):
    """Finding-4: rescuer path also honours the reap grace window.

    Without this the periodic rescuer would fire within 30s of a CSM
    restart and clobber the mission even though startup_reap's own
    grace-skip had spared it. Fresh `last_activity_ts` = restart victim,
    not orphan.
    """
    from csm.modules.workflow import orchestrator as orch_mod

    wf = await _seed_workflow(db)
    ws = tmp_path / "ws-orphan-grace"
    ws.mkdir()
    mission = await _seed_mission(db, wf, workspace_path=str(ws))
    # Fresh activity → grace should skip
    await _seed_session(db, session_id="sess-orphan-grace", pid=424243, cwd=str(ws), activity_age_sec=5)
    await _seed_run(
        db,
        mission,
        stage_name="design",
        session_id="sess-orphan-grace",
        status=RunStatus.RUNNING,
    )

    orch = _make_orch(db)

    def _kill_raises(pid, sig):
        raise ProcessLookupError(f"no process {pid}")

    monkeypatch.setattr(orch_mod.os, "kill", _kill_raises)

    await orch._rescue_pass()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING
        assert m.failure_reason is None


@pytest.mark.asyncio
async def test_rescue_terminated_run_advances_mission(db, tmp_path, monkeypatch):
    """Run.status SUCCEEDED but mission still RUNNING → synthetic SESSION_ENDED
    advances the mission to the next stage.

    Simulates the lost-event edge case: the AUTO session finished and its
    Run was finalised (typically by `AutomationRunner._on_session_ended`),
    but the workflow orchestrator's own subscriber missed the event.
    """
    wf = await _seed_workflow(db)
    ws = tmp_path / "ws-lost-event"
    ws.mkdir()
    mission = await _seed_mission(db, wf, workspace_path=str(ws))
    await _seed_run(
        db,
        mission,
        stage_name="design",
        session_id="sess-lost",
        status=RunStatus.SUCCEEDED,
        exit_code=0,
    )

    orch = _make_orch(db)
    # The advance path should call _start_claude_stage for the next stage.
    # We don't want to actually drive the session manager from this test.
    spawn_spy = AsyncMock()
    monkeypatch.setattr(orch, "_start_claude_stage", spawn_spy)

    await orch._rescue_pass()

    # The synthetic event was routed through the regular pipeline →
    # validation (none configured) passes → advance to "review".
    spawn_spy.assert_awaited_once()
    args, _kwargs = spawn_spy.await_args
    advanced_mission, advanced_stage = args
    assert advanced_mission.id == mission.id
    assert advanced_stage.name == "review"
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING  # advanced, not failed
        assert m.current_stage == "review"
        assert m.audit_log[-1]["event"] == "stage_advanced"
        assert m.audit_log[-1]["from"] == "design"
        assert m.audit_log[-1]["to"] == "review"


@pytest.mark.asyncio
async def test_rescue_healthy_running_mission_untouched(db, tmp_path, monkeypatch):
    """RUNNING mission + RUNNING run + live PID → no transition."""
    from csm.modules.workflow import orchestrator as orch_mod

    wf = await _seed_workflow(db)
    ws = tmp_path / "ws-healthy"
    ws.mkdir()
    mission = await _seed_mission(db, wf, workspace_path=str(ws))
    await _seed_session(db, session_id="sess-alive", pid=12345, cwd=str(ws))
    await _seed_run(
        db,
        mission,
        stage_name="design",
        session_id="sess-alive",
        status=RunStatus.RUNNING,
    )

    orch = _make_orch(db)
    # Spy on every state-changing path — none should fire.
    fail_spy = AsyncMock()
    succ_spy = AsyncMock()
    advance_spy = AsyncMock()
    monkeypatch.setattr(orch, "_finalize_mission_failed", fail_spy)
    monkeypatch.setattr(orch, "_finalize_mission_succeeded", succ_spy)
    monkeypatch.setattr(orch, "_advance_to_next_stage", advance_spy)

    # PID looks alive — os.kill returns normally (no exception raised).
    monkeypatch.setattr(orch_mod.os, "kill", lambda pid, sig: None)

    await orch._rescue_pass()

    fail_spy.assert_not_awaited()
    succ_spy.assert_not_awaited()
    advance_spy.assert_not_awaited()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING
        assert m.current_stage == "design"
        # No audit_log churn either.
        assert m.audit_log is None or all(
            entry["event"] != "failed" for entry in m.audit_log
        )


@pytest.mark.asyncio
async def test_rescue_no_run_for_current_stage_fails_mission(db, tmp_path):
    """Mission claims to be running a stage but no Run row exists → fail."""
    wf = await _seed_workflow(db)
    ws = tmp_path / "ws-no-run"
    ws.mkdir()
    mission = await _seed_mission(db, wf, workspace_path=str(ws))
    # Intentionally NO Run insert.

    orch = _make_orch(db)
    await orch._rescue_pass()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.FAILED
        assert m.failure_reason and "no run for current stage 'design'" in m.failure_reason
        assert m.audit_log[-1]["event"] == "failed"


@pytest.mark.asyncio
async def test_rescue_pass_handles_db_error_gracefully(caplog):
    """A broken sessionmaker must not propagate past `_rescue_pass`.

    The rescuer runs forever in `_rescuer_loop`; a single bad pass should
    log + return so the next 30s tick has a chance to recover (e.g. after
    a transient SQLite WAL lock clears).
    """
    import logging

    caplog.set_level(logging.ERROR, logger="csm.modules.workflow.orchestrator")

    def _explode():
        raise RuntimeError("simulated DB failure")

    broken_sm = MagicMock(side_effect=_explode)
    orch = WorkflowOrchestrator(
        sessionmaker=broken_sm,
        event_stream=MagicMock(),
        session_manager=MagicMock(),
        runner=MagicMock(),
        workflow_loader=MagicMock(),
    )

    # Must not raise.
    await orch._rescue_pass()

    # And it logged the failure rather than silently swallowing.
    assert any(
        "rescuer: top-level select failed" in r.message
        for r in caplog.records
    )


# ----------------------------------------------------------------------------
# Poll-kind stage skip tests (regression: real-run P3 defer, 2026-07-03)
# ----------------------------------------------------------------------------
#
# Before this fix, `_rescue_pass` scanned every RUNNING mission and looked
# up a Run row keyed on `mission.current_stage`. But poll-kind stages are
# driven by `PollExecutor` — an in-memory asyncio loop that never inserts
# Run rows. So the first rescuer tick after a mission advanced into any
# poll stage (`wait_train`, `wait_eval`) unconditionally hit the
# `run is None → finalize_failed` branch, killing the mission mid-flight
# even though the poll loop was ticking normally.
#
# See `.workflow/state/escalations.jsonl` entries dated 2026-07-03 and the
# `real_run_final.md` P3 defer note.


@pytest.mark.asyncio
async def test_rescue_skips_poll_stage_missing_run(db, tmp_path):
    """Mission on a poll stage with no Run row → leave alone.

    Poll stages never produce Run rows; the pre-fix rescuer would fail
    such missions on the first tick after advance.
    """
    wf = await _seed_workflow(db, name="rescuer_poll_stage", yaml_content=_POLL_STAGE_YAML)
    ws = tmp_path / "ws-poll-no-run"
    ws.mkdir()
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_train"
    )
    # Intentionally NO Run insert — that's the whole point of poll stages.

    orch = _make_orch(db)
    await orch._rescue_pass()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING
        assert m.failure_reason is None


# ----------------------------------------------------------------------------
# Backend-review followups (P2-3 coverage gaps)
# ----------------------------------------------------------------------------


async def _seed_workflow_raw(
    sm, *, name: str, yaml_content: str
) -> WorkflowDefinition:
    """Insert a WorkflowDefinition WITHOUT validating yaml_content.

    Needed for broken-spec regression tests — real `_seed_workflow` runs
    `load_workflow_spec` up front which would reject the broken YAML.
    """
    async with sm() as s:
        wf = WorkflowDefinition(
            name=name,
            description=None,
            file_path=f"/tmp/{name}.workflow.yaml",
            yaml_content=yaml_content,
            compiled_rules={"rules": []},
            review_status=WorkflowReviewStatus.PASSED,
        )
        s.add(wf)
        await s.commit()
        await s.refresh(wf)
        return wf


@pytest.mark.asyncio
async def test_rescue_broken_spec_falls_through_to_fail(db, tmp_path):
    """Broken workflow YAML → _stage_kind_from_spec returns None → falls through
    to Run-row check → finalize_failed with workflow_def_id annotation.

    The mission's current_stage name looks like a poll stage, but the spec
    can't be parsed to prove it — so the rescuer conservatively finalizes
    rather than silently ignoring a possibly-stuck mission. Failure reason
    must include workflow_def_id so post-mortem can spot the deleted spec.
    """
    wf = await _seed_workflow_raw(
        db, name="rescuer_broken", yaml_content="::: not valid yaml :::"
    )
    ws = tmp_path / "ws-broken-spec"
    ws.mkdir()
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_train"
    )

    orch = _make_orch(db)
    await orch._rescue_pass()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.FAILED
        assert m.failure_reason is not None
        assert "no run for current stage 'wait_train'" in m.failure_reason
        assert wf.id in m.failure_reason
        assert "workflow spec may be missing or unparseable" in m.failure_reason


@pytest.mark.asyncio
async def test_rescue_current_stage_none_fails_mission(db, tmp_path):
    """Mission RUNNING with current_stage=None → finalize_failed.

    Confirms the "no current_stage" branch of _dispatch_rescue_decision
    (previously untested per backend review P2-3.b).
    """
    wf = await _seed_workflow(db)
    ws = tmp_path / "ws-none-stage"
    ws.mkdir()
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="design"
    )
    # Manually null out current_stage after seeding.
    async with db() as s:
        m = await s.get(Mission, mission.id)
        m.current_stage = None
        await s.commit()

    orch = _make_orch(db)
    await orch._rescue_pass()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.FAILED
        assert "no current_stage" in (m.failure_reason or "")


@pytest.mark.asyncio
async def test_rescue_timeout_on_poll_stage_annotates_reason(db, tmp_path):
    """Global-timeout kill on a poll-stage mission annotates the reason.

    P1-1 from backend review: global_timeout hitting first should not
    silently kill a healthy poll mission — the audit log must surface
    that the stage was poll-kind for post-mortem.
    """
    # workflow with a short global_timeout so the timeout branch fires
    short_yaml = _POLL_STAGE_YAML.replace(
        "stages:", "global_timeout: 1s\nstages:"
    )
    wf = await _seed_workflow(
        db, name="rescuer_poll_timeout", yaml_content=short_yaml
    )
    ws = tmp_path / "ws-poll-timeout"
    ws.mkdir()
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_train"
    )
    # Backdate started_at so the timeout budget is already blown.
    async with db() as s:
        m = await s.get(Mission, mission.id)
        m.started_at = datetime.utcnow() - timedelta(seconds=3600)
        await s.commit()

    orch = _make_orch(db)
    await orch._rescue_pass()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.FAILED
        assert "mission timed out" in (m.failure_reason or "")
        assert "poll-kind" in (m.failure_reason or "")
        assert "wait_train" in (m.failure_reason or "")


@pytest.mark.asyncio
async def test_startup_reap_leaves_poll_stage_mission_alone(db, tmp_path):
    """`_startup_reap` regression: a poll-stage mission with no Run row survives.

    Same-family bug as pre-fix `_rescue_pass`, currently benign because
    the `run is None → continue` skip in _startup_reap catches it. Test
    locks that behaviour so a future "tighter startup recovery" refactor
    can't accidentally reintroduce the bug (backend review P2-1).
    """
    wf = await _seed_workflow(db, name="rescuer_poll_stage", yaml_content=_POLL_STAGE_YAML)
    ws = tmp_path / "ws-startup-reap-poll"
    ws.mkdir()
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_train"
    )
    # Intentionally no Run — poll stages don't produce one.

    orch = _make_orch(db)
    await orch._startup_reap()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING
        assert m.failure_reason is None


@pytest.mark.asyncio
async def test_rescue_skips_poll_stage_with_stale_run(db, tmp_path):
    """Mission on a poll stage that happens to have a stale Run row → still skip.

    Historical data or a hypothetical future code path might leave a Run
    row associated with a poll stage. The rescuer must not act on it —
    poll stages are owned by PollExecutor end-to-end, so any Run row on
    the same (mission, stage) is not authoritative.
    """
    wf = await _seed_workflow(db, name="rescuer_poll_stage", yaml_content=_POLL_STAGE_YAML)
    ws = tmp_path / "ws-poll-stale-run"
    ws.mkdir()
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_train"
    )
    await _seed_run(
        db,
        mission,
        stage_name="wait_train",
        session_id=None,
        status=RunStatus.FAILED,
        exit_code=1,
    )

    orch = _make_orch(db)
    await orch._rescue_pass()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING
        assert m.failure_reason is None

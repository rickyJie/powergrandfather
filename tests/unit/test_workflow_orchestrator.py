"""Tests for WorkflowOrchestrator (M8 night2 T1-T7).

T2 covers the Mission lifecycle (launch / cancel / retry / get + illegal
transitions). T3 adds the SESSION_ENDED-driven advancement path: spawn
first claude stage on launch, run validation primitives on session end,
advance / succeed / fail / defer-poll based on the verdict. T7 adds the
Mission-level timeout (`global_timeout`) check inside `_rescue_pass` and
the one-shot `_startup_reap` invoked from `start()`.

Each test uses an in-memory SQLite (mirrors `tests/unit/test_models_workflow.py`
fixture style) and a minimal WorkflowDefinition row built from a real
`*.workflow.yaml` snippet, so the schema / loader contract stays in the loop.

T7 tests deliberately avoid freezegun (not installed in the csm conda env):
instead, seed `mission.started_at` to `utcnow() - timedelta(seconds=N)` and
pair it with a small `global_timeout` in the workflow YAML so the timeout
predicate `(now - started_at) > global_timeout` is exercised against real
wall-clock arithmetic. `os.kill` is still mocked for the startup-reap PID
probe.
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from csm.core.events import Event, EventType
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
from csm.modules.workflow.orchestrator import (
    InvalidMissionStateTransition,
    WorkflowOrchestrator,
)
from csm.modules.workflow.schema import load_workflow_spec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# ----------------------------------------------------------------------------
# Fixtures
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
    """AsyncMock with a `create_session` that returns a fake Session row.

    The orchestrator's `_start_claude_stage` calls
    `session_manager.create_session(...)` and reads `.id` /
    `.external_session_id` off the return value. We hand back a MagicMock
    with stable ids so the test can match `Run.session_id` later.
    """
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


_SIMPLE_YAML = """\
name: test_wf
description: T2 fixture workflow
parameters:
  - name: topic
    type: string
    required: true
    description: subject under test
  - name: notes
    type: string
    required: false
    default: ""
stages:
  - name: design
    kind: claude
    prompt: "Design for {params.topic}"
    outputs:
      - DESIGN.md
  - name: review
    kind: claude
    prompt: "Review {stages.design.outputs[0]}"
    outputs:
      - REVIEW.md
"""


async def _seed_workflow(
    sm,
    *,
    name: str = "test_wf",
    yaml_content: str = _SIMPLE_YAML,
) -> WorkflowDefinition:
    """Insert a `passed` WorkflowDefinition row built from real YAML."""
    spec = load_workflow_spec(yaml_content)
    # Replace the workflow name only on the YAML so spec.name agrees.
    if spec.name != name:
        raise AssertionError("test bug: name kwarg must match yaml name field")
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
    status: MissionStatus,
    current_stage: str = "design",
    parameters: dict[str, Any] | None = None,
) -> Mission:
    async with sm() as s:
        m = Mission(
            workflow_def_id=wf.id,
            parameters=parameters or {"topic": "x"},
            workspace_path="/tmp/csm-mission-fixture",
            status=status,
            current_stage=current_stage,
        )
        s.add(m)
        await s.commit()
        await s.refresh(m)
        return m


# ----------------------------------------------------------------------------
# T1 smoke (kept)
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_start_stop_smoke(caplog):
    caplog.set_level(logging.INFO, logger="csm.modules.workflow.orchestrator")

    orch = WorkflowOrchestrator(
        sessionmaker=MagicMock(),
        event_stream=MagicMock(),
        session_manager=MagicMock(),
        runner=MagicMock(),
        workflow_loader=MagicMock(),
    )

    await orch.start()
    await orch.stop()

    messages = [r.message for r in caplog.records]
    assert "WorkflowOrchestrator started" in messages
    assert "WorkflowOrchestrator stopped" in messages
    assert orch._subscription_id is None


# ----------------------------------------------------------------------------
# T2 — launch_mission
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_mission_creates_row_in_running(db, tmp_path, monkeypatch):
    # Redirect workspace under tmp_path so we don't litter the repo root.
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    wf = await _seed_workflow(db)
    orch = _make_orch(db)

    mission = await orch.launch_mission("test_wf", {"topic": "sample pipeline"})

    assert mission.id
    assert mission.status == MissionStatus.RUNNING
    assert mission.workflow_def_id == wf.id
    assert mission.current_stage == "design"  # spec.stages[0].name
    assert mission.started_at is not None
    # `notes` has `default: ""` on the spec — launch_mission auto-populates.
    assert mission.parameters == {"topic": "sample pipeline", "notes": ""}
    # workspace dir was actually created
    from pathlib import Path
    assert Path(mission.workspace_path).is_dir()
    # workspace template expanded
    assert mission.id in mission.workspace_path
    # audit_log records pending → running
    assert mission.audit_log and mission.audit_log[-1]["event"] == "launched"
    assert mission.audit_log[-1]["to"] == "running"


@pytest.mark.asyncio
async def test_launch_mission_unknown_workflow_raises(db, tmp_path, monkeypatch):
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    orch = _make_orch(db)
    with pytest.raises(ValueError, match="unknown workflow"):
        await orch.launch_mission("does_not_exist", {})


@pytest.mark.asyncio
async def test_launch_mission_missing_required_param_raises(db, tmp_path, monkeypatch):
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    await _seed_workflow(db)
    orch = _make_orch(db)

    # `topic` is required and has no default — passing empty params must fail.
    with pytest.raises(ValueError, match="missing required parameters"):
        await orch.launch_mission("test_wf", {})

    # Sanity: no mission row was created.
    async with db() as s:
        from sqlalchemy import select
        rows = (await s.execute(select(Mission))).scalars().all()
        assert rows == []


# ----------------------------------------------------------------------------
# T2 — cancel_mission
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_running_transitions_to_cancelled(db):
    wf = await _seed_workflow(db)
    mission = await _seed_mission(db, wf, status=MissionStatus.RUNNING)
    orch = _make_orch(db)

    updated = await orch.cancel_mission(mission.id)

    assert updated.status == MissionStatus.CANCELLED
    assert updated.ended_at is not None
    # transition recorded
    assert updated.audit_log[-1]["event"] == "cancelled"
    assert updated.audit_log[-1]["from"] == "running"
    assert updated.audit_log[-1]["to"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_already_terminal_raises(db):
    wf = await _seed_workflow(db)
    # Cancel-of-cancelled is the cleanest terminal case to assert.
    mission = await _seed_mission(db, wf, status=MissionStatus.CANCELLED)
    orch = _make_orch(db)

    with pytest.raises(InvalidMissionStateTransition, match="cancelled → cancelled"):
        await orch.cancel_mission(mission.id)

    # And the same guard fires for succeeded.
    succeeded = await _seed_mission(db, wf, status=MissionStatus.SUCCEEDED)
    with pytest.raises(InvalidMissionStateTransition, match="succeeded → cancelled"):
        await orch.cancel_mission(succeeded.id)


# ----------------------------------------------------------------------------
# T2 — retry_from_stage
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_failed_resets_to_running(db):
    wf = await _seed_workflow(db)
    mission = await _seed_mission(
        db, wf, status=MissionStatus.FAILED, current_stage="review"
    )
    orch = _make_orch(db)

    updated = await orch.retry_from_stage(mission.id, "design")

    assert updated.status == MissionStatus.RUNNING
    assert updated.current_stage == "design"
    assert updated.ended_at is None
    assert updated.failure_reason is None
    assert updated.audit_log[-1]["event"] == "retried"
    assert updated.audit_log[-1]["from"] == "failed"
    assert updated.audit_log[-1]["stage"] == "design"


@pytest.mark.asyncio
async def test_retry_succeeded_raises(db):
    wf = await _seed_workflow(db)
    mission = await _seed_mission(db, wf, status=MissionStatus.SUCCEEDED)
    orch = _make_orch(db)

    with pytest.raises(InvalidMissionStateTransition, match="succeeded → running"):
        await orch.retry_from_stage(mission.id, "design")


# ----------------------------------------------------------------------------
# T3 — Event-driven advancement (SESSION_ENDED)
# ----------------------------------------------------------------------------

# Two-stage claude→claude workflow with NO validation block (so every stage
# trivially passes and we can isolate the advancement logic from validation
# specifics). Stages use {ws} path placeholders since `outputs` is required.
_T3_TWO_STAGE_YAML = """\
name: t3_two_stage
description: T3 advancement fixture
parameters:
  - name: topic
    type: string
    required: true
stages:
  - name: design
    kind: claude
    prompt: "Design for {params.topic}"
    outputs:
      - "{ws}/design.md"
  - name: review
    kind: claude
    prompt: "Review {stages.design.outputs[0]}"
    outputs:
      - "{ws}/review.md"
"""

# Single-stage workflow for "last stage succeeds" assertion.
_T3_ONE_STAGE_YAML = """\
name: t3_one_stage
description: T3 single-stage fixture
parameters:
  - name: topic
    type: string
    required: true
stages:
  - name: design
    kind: claude
    prompt: "Design for {params.topic}"
    outputs:
      - "{ws}/design.md"
"""

# Validation that targets a file that the test never writes — `file_exists`
# returns False, so the orchestrator must mark the Mission FAILED.
_T3_VALIDATION_FAIL_YAML = """\
name: t3_vfail
description: T3 validation-fail fixture
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
    validation:
      - file: "{ws}/design.md"
        primitives:
          - file_exists
"""

# claude → poll workflow exercises the deferred-poll placeholder branch.
_T3_CLAUDE_THEN_POLL_YAML = """\
name: t3_then_poll
description: T3 claude → poll fixture
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
  - name: wait_thing
    kind: poll
    poll_interval: 60s
    check:
      - file: "{ws}/status.json"
        primitives:
          - file_exists
"""


async def _seed_stage_run(
    sm,
    mission: Mission,
    *,
    stage_name: str,
    session_id: str,
) -> Run:
    """Insert a RUNNING Run row stamped as a mission stage.

    Mirrors the shape `_start_claude_stage` would have written, but without
    spawning a session — the test feeds the orchestrator a synthetic
    SESSION_ENDED event for `session_id` afterwards.
    """
    async with sm() as s:
        run = Run(  # mission-owned Run (no TaskDef binding)
            session_id=session_id,
            status=RunStatus.RUNNING,
            parameters=dict(mission.parameters or {}),
            mission_id=mission.id,
            stage_name=stage_name,
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return run


def _make_session_ended_event(csm_sid: str) -> Event:
    """Construct a synthetic SESSION_ENDED event with the runner's payload shape."""
    return Event(
        type=EventType.SESSION_ENDED,
        ts=datetime.now(UTC),
        session_id="claude-sid-irrelevant",
        project_path=None,
        payload={"csm_session_id": csm_sid, "exit_code": 0},
    )


@pytest.mark.asyncio
async def test_launch_starts_first_claude_stage(db, tmp_path, monkeypatch):
    """launch_mission must spawn the first claude stage's AUTO session."""
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    await _seed_workflow(db)
    orch = _make_orch(db)

    # Spy on _start_claude_stage so we don't have to walk all the way into
    # session_manager.create_session for this assertion. (We still want the
    # real DB write, so we replace just this one method.)
    spy = AsyncMock()
    monkeypatch.setattr(orch, "_start_claude_stage", spy)

    mission = await orch.launch_mission("test_wf", {"topic": "x"})

    spy.assert_awaited_once()
    args, _kwargs = spy.await_args
    spawned_mission, spawned_stage = args
    assert spawned_mission.id == mission.id
    assert spawned_stage.name == "design"
    assert spawned_stage.kind == "claude"


@pytest.mark.asyncio
async def test_session_ended_advances_to_next_stage(db, tmp_path, monkeypatch):
    """Validation passes after first stage → orchestrator starts second stage."""
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    wf = await _seed_workflow(
        db, name="t3_two_stage", yaml_content=_T3_TWO_STAGE_YAML
    )
    mission = await _seed_mission(
        db, wf, status=MissionStatus.RUNNING, current_stage="design"
    )
    run = await _seed_stage_run(
        db, mission, stage_name="design", session_id="sess-design"
    )

    orch = _make_orch(db)
    spy = AsyncMock()
    monkeypatch.setattr(orch, "_start_claude_stage", spy)

    await orch._on_session_ended(_make_session_ended_event("sess-design"))

    spy.assert_awaited_once()
    args, _kwargs = spy.await_args
    spawned_mission, spawned_stage = args
    assert spawned_mission.id == mission.id
    assert spawned_stage.name == "review"
    # Mission was updated to point at the next stage.
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.current_stage == "review"
        assert m.status == MissionStatus.RUNNING
        assert m.audit_log[-1]["event"] == "stage_advanced"
        assert m.audit_log[-1]["from"] == "design"
        assert m.audit_log[-1]["to"] == "review"
    # The completed stage's Run row is not modified by the orchestrator
    # (the AutomationRunner's SESSION_ENDED handler owns Run finalization).
    assert run.status == RunStatus.RUNNING  # in-memory snapshot from seed


@pytest.mark.asyncio
async def test_session_ended_writes_output_rows_for_declared_outputs(
    db, tmp_path, monkeypatch
):
    """After validation passes, one Output row per declared output path.

    Regression against the P4 smoke-time gap where `_collect_prior_outputs`
    returned {} for stage_outputs even though the stage had `outputs:
    - {ws}/design.md` in its spec — nothing wrote the Output row, so
    downstream `{stages.design.outputs[0]}` placeholders failed to bind.
    """
    from csm.models import Output
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    wf = await _seed_workflow(
        db, name="t3_two_stage", yaml_content=_T3_TWO_STAGE_YAML
    )
    mission = await _seed_mission(
        db, wf, status=MissionStatus.RUNNING, current_stage="design"
    )
    # Point mission at a real workspace so {ws}/design.md is renderable.
    ws = tmp_path / "ws-t16"
    ws.mkdir()
    (ws / "design.md").write_text("# design")
    async with db() as s:
        m = await s.get(Mission, mission.id)
        m.workspace_path = str(ws)
        await s.commit()
    run = await _seed_stage_run(
        db, mission, stage_name="design", session_id="sess-design-outputs"
    )

    orch = _make_orch(db)
    monkeypatch.setattr(orch, "_start_claude_stage", AsyncMock())

    await orch._on_session_ended(_make_session_ended_event("sess-design-outputs"))

    async with db() as s:
        rows = (await s.execute(select(Output).where(Output.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].path == str(ws / "design.md")
    # File exists → preview should be non-null (`_safe_preview` reads it).
    assert rows[0].preview is not None
    assert "design" in rows[0].preview


@pytest.mark.asyncio
async def test_session_ended_output_row_written_even_when_file_missing(
    db, tmp_path, monkeypatch
):
    """Declared output whose file wasn't actually produced still gets a row.

    Design choice per `_persist_stage_outputs` docstring: validation has
    just passed, so any missing file is either optional or arrives async;
    we still record the path so downstream stages can reference it.
    Preview is None when the file doesn't exist.
    """
    from csm.models import Output
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    # Use the single-stage YAML so there's no next-stage spawn logic to mock.
    wf = await _seed_workflow(
        db, name="t3_one_stage", yaml_content=_T3_ONE_STAGE_YAML
    )
    mission = await _seed_mission(
        db, wf, status=MissionStatus.RUNNING, current_stage="design"
    )
    ws = tmp_path / "ws-t16-missing"
    ws.mkdir()
    # Intentionally do NOT create design.md — but the one-stage YAML has
    # no validation block that requires it, so validation passes vacuously.
    async with db() as s:
        m = await s.get(Mission, mission.id)
        m.workspace_path = str(ws)
        await s.commit()
    run = await _seed_stage_run(
        db, mission, stage_name="design", session_id="sess-missing-file"
    )

    orch = _make_orch(db)

    await orch._on_session_ended(_make_session_ended_event("sess-missing-file"))

    async with db() as s:
        rows = (await s.execute(select(Output).where(Output.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].path == str(ws / "design.md")
    assert rows[0].preview is None  # file didn't exist


@pytest.mark.asyncio
async def test_session_ended_validation_fail_marks_mission_failed(
    db, tmp_path, monkeypatch
):
    """A stage whose validation block reports failure must fail the Mission."""
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    wf = await _seed_workflow(
        db, name="t3_vfail", yaml_content=_T3_VALIDATION_FAIL_YAML
    )
    # workspace_path points at an empty tmp dir — design.md doesn't exist.
    mission = await _seed_mission(
        db, wf, status=MissionStatus.RUNNING, current_stage="design"
    )
    async with db() as s:
        m = await s.get(Mission, mission.id)
        m.workspace_path = str(tmp_path / "empty-ws")
        (tmp_path / "empty-ws").mkdir()
        await s.commit()
    await _seed_stage_run(db, mission, stage_name="design", session_id="sess-vf")

    orch = _make_orch(db)
    spy = AsyncMock()
    monkeypatch.setattr(orch, "_start_claude_stage", spy)

    await orch._on_session_ended(_make_session_ended_event("sess-vf"))

    spy.assert_not_awaited()  # no next-stage spawn on failure
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.FAILED
        assert m.ended_at is not None
        assert m.failure_reason and "validation failed" in m.failure_reason
        assert "design" in m.failure_reason
        assert m.audit_log[-1]["event"] == "failed"
        assert m.audit_log[-1]["to"] == "failed"


@pytest.mark.asyncio
async def test_session_ended_last_stage_marks_mission_succeeded(
    db, tmp_path, monkeypatch
):
    """When the just-ended stage was the final one, Mission → succeeded."""
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    wf = await _seed_workflow(
        db, name="t3_one_stage", yaml_content=_T3_ONE_STAGE_YAML
    )
    mission = await _seed_mission(
        db, wf, status=MissionStatus.RUNNING, current_stage="design"
    )
    await _seed_stage_run(db, mission, stage_name="design", session_id="sess-final")

    orch = _make_orch(db)
    spy = AsyncMock()
    monkeypatch.setattr(orch, "_start_claude_stage", spy)

    await orch._on_session_ended(_make_session_ended_event("sess-final"))

    spy.assert_not_awaited()  # no further stage to spawn
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.SUCCEEDED
        assert m.ended_at is not None
        assert m.failure_reason is None
        assert m.audit_log[-1]["event"] == "succeeded"
        assert m.audit_log[-1]["to"] == "succeeded"


@pytest.mark.asyncio
async def test_stage_assistant_done_stops_session_after_grace(db, tmp_path, monkeypatch):
    """Finding-8: after claude's end_turn, orchestrator must stop the stage's
    AUTO session so PTY EOF fires SESSION_ENDED and the mission advances.

    Without this fix the claude REPL sits alive indefinitely after end_turn
    (test-run-2 observed a stage session held for 20+ min until manually
    killed), and every mission ends up ambient-timing-out at global_timeout
    even when its work finished in seconds.
    """
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    sm_stub = AsyncMock()
    sm_stub.stop_session = AsyncMock(return_value=0)
    orch = _make_orch(db, session_manager=sm_stub)
    orch._orch_grace_sec = 0.05  # fast test

    # Register a fake orchestrator-owned session (external_sid → csm_sid).
    orch._orch_csm_by_external_session["claude-fake"] = "sess-fake"

    ev = Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.utcnow(),
        session_id="claude-fake",
        project_path=str(tmp_path),
        payload={"model": "claude-sonnet-4-6"},
    )
    await orch._on_stage_assistant_done(ev)
    # Grace timer is armed but not yet fired.
    sm_stub.stop_session.assert_not_awaited()
    # Wait past grace.
    import asyncio as _asyncio
    await _asyncio.sleep(0.15)
    sm_stub.stop_session.assert_awaited_once_with("sess-fake", graceful=True)
    # Map is untouched — cleanup only happens on SESSION_ENDED so a spurious
    # second end_turn during the grace window can still re-arm.
    assert "claude-fake" in orch._orch_csm_by_external_session


async def test_stage_session_idle_stops_session(db, tmp_path, monkeypatch):
    """Finding-8b: SESSION_IDLE for a registered orchestrator stage session
    must force-stop it (backstop for claude that never emits end_turn — rate
    limit, crash, hung tool).
    """
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    sm_stub = AsyncMock()
    sm_stub.stop_session = AsyncMock(return_value=0)
    orch = _make_orch(db, session_manager=sm_stub)
    orch._orch_csm_by_external_session["claude-crashed"] = "sess-crashed"

    ev = Event(
        type=EventType.SESSION_IDLE,
        ts=datetime.utcnow(),
        session_id="claude-crashed",
        project_path=str(tmp_path),
        payload={"idle_seconds": 1900, "jsonl_path": "/tmp/x.jsonl"},
    )
    await orch._on_stage_session_idle(ev)
    sm_stub.stop_session.assert_awaited_once_with("sess-crashed", graceful=True)


async def test_stage_session_idle_cancels_pending_grace(db, tmp_path, monkeypatch):
    """If a grace timer is armed when SESSION_IDLE fires (rare race where
    end_turn and idle both reach us), the idle path must cancel the grace
    so we don't stop_session twice."""
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    sm_stub = AsyncMock()
    sm_stub.stop_session = AsyncMock(return_value=0)
    orch = _make_orch(db, session_manager=sm_stub)
    orch._orch_grace_sec = 5.0
    orch._orch_csm_by_external_session["claude-race"] = "sess-race"
    import asyncio as _asyncio
    orch._orch_grace_tasks["sess-race"] = _asyncio.create_task(_asyncio.sleep(5))

    ev = Event(
        type=EventType.SESSION_IDLE,
        ts=datetime.utcnow(),
        session_id="claude-race",
        project_path=str(tmp_path),
        payload={"idle_seconds": 1900},
    )
    await orch._on_stage_session_idle(ev)
    assert "sess-race" not in orch._orch_grace_tasks
    sm_stub.stop_session.assert_awaited_once()


async def test_stage_session_idle_ignores_unknown_session(db, tmp_path, monkeypatch):
    """SESSION_IDLE for a claude session the orchestrator didn't spawn must
    no-op — runner's handler owns that one."""
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    sm_stub = AsyncMock()
    sm_stub.stop_session = AsyncMock(return_value=0)
    orch = _make_orch(db, session_manager=sm_stub)

    ev = Event(
        type=EventType.SESSION_IDLE,
        ts=datetime.utcnow(),
        session_id="not-ours",
        project_path=str(tmp_path),
        payload={"idle_seconds": 1900},
    )
    await orch._on_stage_session_idle(ev)
    sm_stub.stop_session.assert_not_awaited()


async def test_stage_assistant_done_ignores_unknown_session(db, tmp_path, monkeypatch):
    """MESSAGE_ASSISTANT_DONE for a claude session the orchestrator didn't
    spawn must no-op — otherwise we'd racy-stop runner-owned sessions."""
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    sm_stub = AsyncMock()
    sm_stub.stop_session = AsyncMock(return_value=0)
    orch = _make_orch(db, session_manager=sm_stub)
    orch._orch_grace_sec = 0.05

    ev = Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.utcnow(),
        session_id="not-ours",
        project_path=str(tmp_path),
        payload={},
    )
    await orch._on_stage_assistant_done(ev)
    import asyncio as _asyncio
    await _asyncio.sleep(0.1)
    sm_stub.stop_session.assert_not_awaited()


async def test_session_ended_non_mission_session_ignored(
    db, tmp_path, monkeypatch
):
    """SESSION_ENDED for a non-mission Run (or unknown session) must no-op.

    Ensures the orchestrator's handler doesn't accidentally interfere with
    AutomationRunner's normal task runs.
    """
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    wf = await _seed_workflow(
        db, name="t3_one_stage", yaml_content=_T3_ONE_STAGE_YAML
    )
    mission = await _seed_mission(
        db, wf, status=MissionStatus.RUNNING, current_stage="design"
    )

    # Insert a Run with mission_id=None (a regular TaskDef-driven Run) plus
    # an unrelated mission Run that the event does NOT target — neither
    # should be touched.
    async with db() as s:
        plain = Run(
            session_id="sess-plain",
            status=RunStatus.RUNNING,
            parameters={},
            mission_id=None,
        )
        s.add(plain)
        await s.commit()

    orch = _make_orch(db)
    spy = AsyncMock()
    monkeypatch.setattr(orch, "_start_claude_stage", spy)
    fail_spy = AsyncMock()
    monkeypatch.setattr(orch, "_finalize_mission_failed", fail_spy)
    succ_spy = AsyncMock()
    monkeypatch.setattr(orch, "_finalize_mission_succeeded", succ_spy)

    # (a) event for the non-mission Run
    await orch._on_session_ended(_make_session_ended_event("sess-plain"))
    # (b) event for an entirely unknown session
    await orch._on_session_ended(_make_session_ended_event("sess-ghost"))
    # (c) event with no csm_session_id at all (malformed payload)
    await orch._on_session_ended(
        Event(
            type=EventType.SESSION_ENDED,
            ts=datetime.now(UTC),
            session_id=None,
            project_path=None,
            payload={},
        )
    )

    spy.assert_not_awaited()
    fail_spy.assert_not_awaited()
    succ_spy.assert_not_awaited()
    # Mission status untouched
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING


@pytest.mark.asyncio
async def test_poll_stage_starts_poll_loop(
    db, tmp_path, monkeypatch
):
    """When the next stage after a passed claude stage is `kind: poll`, the
    orchestrator must hand off to its `PollExecutor.start_poll(...)` rather
    than fail the Mission. The Mission's `current_stage` advances to the
    poll stage and the row stays RUNNING; the asyncio loop's verdict is
    PollExecutor's concern (covered by `test_workflow_poll_executor.py`).
    """
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    wf = await _seed_workflow(
        db, name="t3_then_poll", yaml_content=_T3_CLAUDE_THEN_POLL_YAML
    )
    mission = await _seed_mission(
        db, wf, status=MissionStatus.RUNNING, current_stage="design"
    )
    await _seed_stage_run(db, mission, stage_name="design", session_id="sess-cdp")

    orch = _make_orch(db)
    start_claude_spy = AsyncMock()
    monkeypatch.setattr(orch, "_start_claude_stage", start_claude_spy)
    start_poll_spy = AsyncMock()
    monkeypatch.setattr(orch._poll_executor, "start_poll", start_poll_spy)

    await orch._on_session_ended(_make_session_ended_event("sess-cdp"))

    # No claude session spawn — the next stage is poll.
    start_claude_spy.assert_not_awaited()
    # PollExecutor was handed the (mission_id, stage_name, stage_spec) triple.
    start_poll_spy.assert_awaited_once()
    args, _kwargs = start_poll_spy.await_args
    sent_mid, sent_stage_name, sent_stage_spec = args
    assert sent_mid == mission.id
    assert sent_stage_name == "wait_thing"
    assert sent_stage_spec.kind == "poll"

    # Mission rolled forward to the poll stage and is still RUNNING.
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING
        assert m.current_stage == "wait_thing"
        assert m.audit_log[-1]["event"] == "stage_advanced"
        assert m.audit_log[-1]["from"] == "design"
        assert m.audit_log[-1]["to"] == "wait_thing"


# ----------------------------------------------------------------------------
# T4 — STATE.yaml writer
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_yaml_written_on_launch(db, tmp_path, monkeypatch):
    """launch_mission must create <ws>/STATE.yaml with the initial snapshot."""
    import yaml
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    await _seed_workflow(db)
    orch = _make_orch(db)
    # Stop the spawn path — we only care that STATE.yaml lands on disk.
    monkeypatch.setattr(orch, "_start_claude_stage", AsyncMock())

    mission = await orch.launch_mission("test_wf", {"topic": "sample"})

    state_path = Path(mission.workspace_path) / "STATE.yaml"
    assert state_path.is_file(), f"STATE.yaml missing at {state_path}"
    raw = state_path.read_text(encoding="utf-8")
    assert raw.startswith("# auto-generated by CSM orchestrator")
    snap = yaml.safe_load(raw)
    assert snap["mission_id"] == mission.id
    assert snap["workflow_name"] == "test_wf"
    assert snap["status"] == "running"
    assert snap["current_stage"] == "design"
    # `notes` default: "" from spec, auto-populated at launch.
    assert snap["params"] == {"topic": "sample", "notes": ""}
    stages = snap["stages"]
    assert [s["name"] for s in stages] == ["design", "review"]
    # No Run rows exist yet (spawn was stubbed), so both stages are pending.
    assert all(s["status"] == "pending" for s in stages)
    # Output templates with {ws} / {stages.X.outputs[N]} get partial substitution:
    # {ws} resolves to the workspace path; cross-stage refs stay as-is in the
    # informational snapshot (they're not a live prompt).
    assert stages[0]["outputs"] == ["DESIGN.md"]
    assert stages[1]["outputs"] == ["REVIEW.md"]


@pytest.mark.asyncio
async def test_state_yaml_updated_on_stage_advance(db, tmp_path, monkeypatch):
    """After a stage advance the rewritten snapshot must reflect the new current_stage."""
    import yaml
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    wf = await _seed_workflow(
        db, name="t3_two_stage", yaml_content=_T3_TWO_STAGE_YAML
    )
    # Point the mission's workspace at a real tmp directory we own.
    ws = tmp_path / "ws-state-advance"
    ws.mkdir()
    mission = await _seed_mission(
        db, wf, status=MissionStatus.RUNNING, current_stage="design"
    )
    async with db() as s:
        m = await s.get(Mission, mission.id)
        m.workspace_path = str(ws)
        m.started_at = datetime(2026, 1, 1, 12, 0, 0)
        await s.commit()
    await _seed_stage_run(db, mission, stage_name="design", session_id="sess-adv")

    orch = _make_orch(db)
    # Stop the next-stage spawn — we just want the advance UPDATE + STATE.yaml.
    monkeypatch.setattr(orch, "_start_claude_stage", AsyncMock())

    await orch._on_session_ended(_make_session_ended_event("sess-adv"))

    state_path = ws / "STATE.yaml"
    assert state_path.is_file()
    snap = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    assert snap["current_stage"] == "review"
    assert snap["status"] == "running"
    # The design stage transitioned to succeeded on advance; the review
    # stage is next (pending until the runner spawns it).
    by_name = {s["name"]: s for s in snap["stages"]}
    assert by_name["design"]["status"] == "succeeded"
    assert by_name["design"]["started_at"] is not None
    assert by_name["review"]["status"] == "pending"


# ----------------------------------------------------------------------------
# T7 — Mission timeout + startup reap
# ----------------------------------------------------------------------------

# Small global_timeout makes the rescuer's timeout predicate easy to trip
# with a backdated `mission.started_at`. The default (604800s == 7 days)
# is used by `_T7_DEFAULT_TIMEOUT_YAML` to exercise the "no timeout fires
# for a fresh mission" path.
_T7_TIGHT_TIMEOUT_YAML = """\
name: t7_tight
description: T7 timeout fixture (60s budget)
parameters:
  - name: topic
    type: string
    required: true
global_timeout: 60s
stages:
  - name: design
    kind: claude
    prompt: "Design"
    outputs:
      - "{ws}/design.md"
"""

_T7_DEFAULT_TIMEOUT_YAML = """\
name: t7_default
description: T7 fixture with default 7-day timeout
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
"""


async def _seed_session_for_t7(
    sm,
    *,
    session_id: str,
    pid: int | None,
    cwd: str,
) -> Session:
    """Insert a Session row with a fixed PID for orphan-reap / timeout tests."""
    async with sm() as s:
        sess = Session(
            id=session_id,
            title="t7-fixture",
            type=SessionType.AUTO,
            cwd=cwd,
            status=SessionStatus.RUNNING,
            pid=pid,
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        return sess


async def _backdate_mission_started_at(sm, mission_id: str, seconds_ago: int) -> None:
    """Move `mission.started_at` `seconds_ago` seconds into the past.

    Replaces freezegun: the rescuer reads wall-clock `utcnow()`, so to
    cross the timeout boundary we change the *mission* anchor instead.
    """
    async with sm() as s:
        m = await s.get(Mission, mission_id)
        m.started_at = datetime.utcnow() - timedelta(seconds=seconds_ago)
        await s.commit()


@pytest.mark.asyncio
async def test_mission_exceeds_max_duration_marked_failed(db, tmp_path, monkeypatch):
    """global_timeout=60s + started_at=10000s ago → rescuer fails the mission."""
    wf = await _seed_workflow(
        db, name="t7_tight", yaml_content=_T7_TIGHT_TIMEOUT_YAML
    )
    ws = tmp_path / "ws-timeout"
    ws.mkdir()
    mission = Mission(
        workflow_def_id=wf.id,
        parameters={"topic": "x"},
        workspace_path=str(ws),
        status=MissionStatus.RUNNING,
        current_stage="design",
        started_at=datetime.utcnow(),
    )
    async with db() as s:
        s.add(mission)
        await s.commit()
        await s.refresh(mission)
    await _backdate_mission_started_at(db, mission.id, seconds_ago=10_000)

    await _seed_session_for_t7(
        db, session_id="sess-timeout", pid=4242, cwd=str(ws)
    )
    async with db() as s:
        run = Run(
            session_id="sess-timeout",
            status=RunStatus.RUNNING,
            parameters={"topic": "x"},
            mission_id=mission.id,
            stage_name="design",
        )
        s.add(run)
        await s.commit()

    # Hand the session manager a stub so the rescuer can issue stop_session.
    sm_stub = AsyncMock()
    sm_stub.stop_session = AsyncMock(return_value=0)
    orch = _make_orch(db, session_manager=sm_stub)

    # PID looks alive so the timeout dispatch goes through the
    # stop_session call path (rather than the "already dead" skip).
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.os, "kill", lambda pid, sig: None)

    await orch._rescue_pass()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.FAILED
        assert m.ended_at is not None
        assert m.failure_reason == "mission timed out at 60s"
        assert m.audit_log[-1]["event"] == "failed"
        assert "timed out at 60s" in m.audit_log[-1]["reason"]
    # The live session got the SIGTERM call.
    sm_stub.stop_session.assert_awaited_once_with("sess-timeout")


@pytest.mark.asyncio
async def test_mission_under_max_duration_unaffected(db, tmp_path, monkeypatch):
    """global_timeout=60s + started_at=10s ago → no timeout dispatch.

    A healthy RUNNING mission whose stage has a live PID stays RUNNING.
    The rescuer's run-state branch sees the live PID and leaves it alone.
    """
    wf = await _seed_workflow(
        db, name="t7_tight", yaml_content=_T7_TIGHT_TIMEOUT_YAML
    )
    ws = tmp_path / "ws-under"
    ws.mkdir()
    mission = Mission(
        workflow_def_id=wf.id,
        parameters={"topic": "x"},
        workspace_path=str(ws),
        status=MissionStatus.RUNNING,
        current_stage="design",
        started_at=datetime.utcnow() - timedelta(seconds=10),
    )
    async with db() as s:
        s.add(mission)
        await s.commit()
        await s.refresh(mission)

    await _seed_session_for_t7(
        db, session_id="sess-under", pid=4243, cwd=str(ws)
    )
    async with db() as s:
        run = Run(
            session_id="sess-under",
            status=RunStatus.RUNNING,
            parameters={"topic": "x"},
            mission_id=mission.id,
            stage_name="design",
        )
        s.add(run)
        await s.commit()

    sm_stub = AsyncMock()
    sm_stub.stop_session = AsyncMock()
    orch = _make_orch(db, session_manager=sm_stub)
    # Spy on the timeout-dispatch path to assert it's NOT awaited.
    timeout_spy = AsyncMock()
    monkeypatch.setattr(orch, "_dispatch_timeout", timeout_spy)
    # PID alive → run-state branch leaves the mission alone.
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.os, "kill", lambda pid, sig: None)

    await orch._rescue_pass()

    timeout_spy.assert_not_awaited()
    sm_stub.stop_session.assert_not_awaited()
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING
        assert m.failure_reason is None


@pytest.mark.asyncio
async def test_no_max_duration_means_no_timeout(db, tmp_path, monkeypatch):
    """Default global_timeout (7 days) → fresh mission never times out.

    The workflow YAML omits `global_timeout`; PRD §3 says the default is
    7 days. A mission backdated 1 hour against that default is nowhere
    near the budget, so no timeout fires even though the rescuer scans it.
    """
    wf = await _seed_workflow(
        db, name="t7_default", yaml_content=_T7_DEFAULT_TIMEOUT_YAML
    )
    ws = tmp_path / "ws-no-timeout"
    ws.mkdir()
    mission = Mission(
        workflow_def_id=wf.id,
        parameters={"topic": "x"},
        workspace_path=str(ws),
        status=MissionStatus.RUNNING,
        current_stage="design",
        started_at=datetime.utcnow() - timedelta(hours=1),
    )
    async with db() as s:
        s.add(mission)
        await s.commit()
        await s.refresh(mission)

    await _seed_session_for_t7(
        db, session_id="sess-fresh", pid=4244, cwd=str(ws)
    )
    async with db() as s:
        run = Run(
            session_id="sess-fresh",
            status=RunStatus.RUNNING,
            parameters={"topic": "x"},
            mission_id=mission.id,
            stage_name="design",
        )
        s.add(run)
        await s.commit()

    sm_stub = AsyncMock()
    sm_stub.stop_session = AsyncMock()
    orch = _make_orch(db, session_manager=sm_stub)
    timeout_spy = AsyncMock()
    monkeypatch.setattr(orch, "_dispatch_timeout", timeout_spy)
    # PID alive — run-state branch is a no-op.
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.os, "kill", lambda pid, sig: None)

    await orch._rescue_pass()

    timeout_spy.assert_not_awaited()
    sm_stub.stop_session.assert_not_awaited()
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING


@pytest.mark.asyncio
async def test_startup_reap_marks_orphan_running_failed(db, tmp_path, monkeypatch):
    """RUNNING mission whose stage's PID is dead at start() → reaped to FAILED."""
    wf = await _seed_workflow(
        db, name="t7_default", yaml_content=_T7_DEFAULT_TIMEOUT_YAML
    )
    ws = tmp_path / "ws-reap-orphan"
    ws.mkdir()
    mission = Mission(
        workflow_def_id=wf.id,
        parameters={"topic": "x"},
        workspace_path=str(ws),
        status=MissionStatus.RUNNING,
        current_stage="design",
        started_at=datetime.utcnow(),
    )
    async with db() as s:
        s.add(mission)
        await s.commit()
        await s.refresh(mission)
    await _seed_session_for_t7(
        db, session_id="sess-orphan-reap", pid=99991, cwd=str(ws)
    )
    # Finding-4: reap has a 60s grace window that keys off Session.last_activity_ts
    # (and started_at as fallback). Backdate both so this test still exercises
    # the reap path — otherwise the fresh session would be grace-skipped.
    async with db() as s:
        row = await s.get(Session, "sess-orphan-reap")
        old = datetime.utcnow() - timedelta(seconds=600)
        row.started_at = old
        row.last_activity_ts = old
        await s.commit()
    async with db() as s:
        run = Run(
            session_id="sess-orphan-reap",
            status=RunStatus.RUNNING,
            parameters={"topic": "x"},
            mission_id=mission.id,
            stage_name="design",
        )
        s.add(run)
        await s.commit()

    orch = _make_orch(db)
    from csm.modules.workflow import orchestrator as orch_mod

    def _kill_raises(pid, sig):
        raise ProcessLookupError(f"no process {pid}")

    monkeypatch.setattr(orch_mod.os, "kill", _kill_raises)

    await orch._startup_reap()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.FAILED
        assert m.ended_at is not None
        assert m.failure_reason and "reaped on startup" in m.failure_reason
        assert "pid=99991" in m.failure_reason
        assert m.audit_log[-1]["event"] == "failed"
        assert "reaped on startup" in m.audit_log[-1]["reason"]


@pytest.mark.asyncio
async def test_startup_reap_grace_skips_recent_activity(db, tmp_path, monkeypatch):
    """Finding-4 regression: pid-dead + recent hook activity → grace-skip, not reap.

    A short CSM restart used to instantly clobber every in-flight mission
    because the startup reap treated pid-dead as terminal. With the 60s
    grace window a session whose last recorded activity is within a
    minute of "now" is left RUNNING; the next rescuer tick re-evaluates
    once the grace has expired, so the fix defers — never absolves — the
    orphan decision.
    """
    wf = await _seed_workflow(
        db, name="t7_default", yaml_content=_T7_DEFAULT_TIMEOUT_YAML
    )
    ws = tmp_path / "ws-reap-grace"
    ws.mkdir()
    mission = Mission(
        workflow_def_id=wf.id,
        parameters={"topic": "x"},
        workspace_path=str(ws),
        status=MissionStatus.RUNNING,
        current_stage="design",
        started_at=datetime.utcnow(),
    )
    async with db() as s:
        s.add(mission)
        await s.commit()
        await s.refresh(mission)
    sess = await _seed_session_for_t7(
        db, session_id="sess-grace-reap", pid=99993, cwd=str(ws)
    )
    # Fresh hook activity = simulates the "CSM stopped 2s ago" restart case.
    async with db() as s:
        row = await s.get(Session, sess.id)
        row.last_activity_ts = datetime.utcnow()
        await s.commit()
    async with db() as s:
        run = Run(
            session_id="sess-grace-reap",
            status=RunStatus.RUNNING,
            parameters={"topic": "x"},
            mission_id=mission.id,
            stage_name="design",
        )
        s.add(run)
        await s.commit()

    orch = _make_orch(db)
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    await orch._startup_reap()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING
        assert m.failure_reason is None


@pytest.mark.asyncio
async def test_startup_reap_grace_expired_still_reaps(db, tmp_path, monkeypatch):
    """Finding-4 upper bound: once activity is older than the grace, reap fires.

    Guards against the grace fix silently swallowing real orphans. A
    mission whose session has been silent well past the 60s window must
    still be finalised FAILED with the "reaped on startup" reason.
    """
    wf = await _seed_workflow(
        db, name="t7_default", yaml_content=_T7_DEFAULT_TIMEOUT_YAML
    )
    ws = tmp_path / "ws-reap-stale"
    ws.mkdir()
    mission = Mission(
        workflow_def_id=wf.id,
        parameters={"topic": "x"},
        workspace_path=str(ws),
        status=MissionStatus.RUNNING,
        current_stage="design",
        started_at=datetime.utcnow(),
    )
    async with db() as s:
        s.add(mission)
        await s.commit()
        await s.refresh(mission)
    sess = await _seed_session_for_t7(
        db, session_id="sess-stale-reap", pid=99994, cwd=str(ws)
    )
    # Push last activity + started_at 10 minutes into the past — well past grace.
    async with db() as s:
        row = await s.get(Session, sess.id)
        old = datetime.utcnow() - timedelta(seconds=600)
        row.last_activity_ts = old
        row.started_at = old
        await s.commit()
    async with db() as s:
        run = Run(
            session_id="sess-stale-reap",
            status=RunStatus.RUNNING,
            parameters={"topic": "x"},
            mission_id=mission.id,
            stage_name="design",
        )
        s.add(run)
        await s.commit()

    orch = _make_orch(db)
    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    await orch._startup_reap()

    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.FAILED
        assert "reaped on startup" in (m.failure_reason or "")


@pytest.mark.asyncio
async def test_startup_reap_skips_healthy_missions(db, tmp_path, monkeypatch):
    """RUNNING mission whose stage PID is alive at start() → untouched."""
    wf = await _seed_workflow(
        db, name="t7_default", yaml_content=_T7_DEFAULT_TIMEOUT_YAML
    )
    ws = tmp_path / "ws-reap-healthy"
    ws.mkdir()
    mission = Mission(
        workflow_def_id=wf.id,
        parameters={"topic": "x"},
        workspace_path=str(ws),
        status=MissionStatus.RUNNING,
        current_stage="design",
        started_at=datetime.utcnow(),
    )
    async with db() as s:
        s.add(mission)
        await s.commit()
        await s.refresh(mission)
    await _seed_session_for_t7(
        db, session_id="sess-healthy", pid=99992, cwd=str(ws)
    )
    async with db() as s:
        run = Run(
            session_id="sess-healthy",
            status=RunStatus.RUNNING,
            parameters={"topic": "x"},
            mission_id=mission.id,
            stage_name="design",
        )
        s.add(run)
        await s.commit()

    orch = _make_orch(db)
    # Spy on every state-changing path so we can prove the reap was a no-op.
    fail_spy = AsyncMock()
    monkeypatch.setattr(orch, "_finalize_mission_failed", fail_spy)

    from csm.modules.workflow import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.os, "kill", lambda pid, sig: None)

    await orch._startup_reap()

    fail_spy.assert_not_awaited()
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.status == MissionStatus.RUNNING
        assert m.failure_reason is None


@pytest.mark.asyncio
async def test_startup_reap_runs_once_not_on_every_pass(db, tmp_path, monkeypatch):
    """`start()` calls `_startup_reap` exactly once; `_rescue_pass` never does.

    Without this guarantee the periodic loop would re-trigger startup-reap
    semantics on every 30s tick — that would conflict with the regular
    rescue decision table (e.g. "no run for current stage" would also
    fire for a fresh mission mid-launch).
    """
    orch = _make_orch(db)

    # Replace _startup_reap with a counter spy.
    reap_spy = AsyncMock()
    monkeypatch.setattr(orch, "_startup_reap", reap_spy)
    # Replace the rescuer task body with an immediate exit so start()
    # doesn't keep a background coroutine alive for the test.
    monkeypatch.setattr(orch, "_rescuer_loop", AsyncMock())

    await orch.start()
    try:
        # Sanity: subscription + reap both fired exactly once during start.
        assert reap_spy.await_count == 1

        # Now run a few `_rescue_pass` cycles directly — none of them
        # should call `_startup_reap`.
        for _ in range(3):
            await orch._rescue_pass()
        assert reap_spy.await_count == 1
    finally:
        await orch.stop()


# ----------------------------------------------------------------------------
# Slot 9 safety net — direct tests for extracted modules
# (orchestrator_reaper.py + orchestrator_state.py). These pin the public
# API surface + delegator wiring so a future refactor that quietly renames
# an extracted fn or drops a delegator method trips a red test before it
# reaches production.
# ----------------------------------------------------------------------------


def test_reaper_module_exports_public_api():
    """orchestrator_reaper must expose the 4 async fns the orchestrator delegators call.

    Regression guard: slot 8's split moved the bodies out but kept the
    delegator methods on WorkflowOrchestrator. If a later cleanup ever
    renames or drops an extracted fn, this catches it before the
    delegator raises AttributeError at runtime.
    """
    import inspect

    from csm.modules.workflow import orchestrator_reaper as _reaper

    for name in (
        "rescuer_loop",
        "rescue_pass",
        "startup_reap",
        "finalize_mission_succeeded",
        "finalize_mission_failed",
    ):
        fn = getattr(_reaper, name, None)
        assert fn is not None, f"orchestrator_reaper.{name} missing"
        assert inspect.iscoroutinefunction(fn), (
            f"orchestrator_reaper.{name} must be async"
        )
    # Ensure __all__ is accurate (protects against public-API drift).
    assert set(_reaper.__all__) >= {
        "rescuer_loop",
        "rescue_pass",
        "startup_reap",
        "finalize_mission_succeeded",
        "finalize_mission_failed",
    }


def test_state_module_exports_public_api():
    """orchestrator_state must expose write_state_yaml + build_state_snapshot."""
    import inspect

    from csm.modules.workflow import orchestrator_state as _state

    for name in ("write_state_yaml", "build_state_snapshot"):
        fn = getattr(_state, name, None)
        assert fn is not None, f"orchestrator_state.{name} missing"
        assert inspect.iscoroutinefunction(fn), (
            f"orchestrator_state.{name} must be async"
        )
    # STATE_YAML constants must remain stable — external tools grep for them.
    assert _state.STATE_YAML_FILENAME == "STATE.yaml"
    assert _state.STATE_YAML_HEADER.startswith("# auto-generated by CSM")


@pytest.mark.asyncio
async def test_state_yaml_write_creates_file(db, tmp_path, monkeypatch):
    """orchestrator_state.write_state_yaml actually writes STATE.yaml under workspace."""
    import yaml as _yaml
    from csm.modules.workflow import orchestrator as orch_mod
    from csm.modules.workflow import orchestrator_state as _state

    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)

    wf = await _seed_workflow(db)
    ws = tmp_path / "ws-state-yaml"
    ws.mkdir()
    async with db() as s:
        m = Mission(
            workflow_def_id=wf.id,
            parameters={"topic": "x", "notes": ""},
            workspace_path=str(ws),
            status=MissionStatus.RUNNING,
            current_stage="design",
        )
        s.add(m)
        await s.commit()
        await s.refresh(m)
        mission_id = m.id

    orch = _make_orch(db)
    # Direct call — bypass the delegator to prove the module fn works standalone.
    await _state.write_state_yaml(orch, mission_id)

    state_path = ws / "STATE.yaml"
    assert state_path.exists(), "write_state_yaml did not create STATE.yaml"
    body = state_path.read_text(encoding="utf-8")
    assert body.startswith(_state.STATE_YAML_HEADER)
    data = _yaml.safe_load(body)
    assert data["mission_id"] == mission_id
    assert data["workflow_name"] == "test_wf"
    assert data["status"] == "running"
    stage_names = [st["name"] for st in data["stages"]]
    assert stage_names == ["design", "review"]


@pytest.mark.asyncio
async def test_state_yaml_write_missing_mission_is_noop(db, tmp_path, monkeypatch):
    """Negative case — write_state_yaml on a bogus mission id silently no-ops.

    Justification: the caller invokes this after every state-changing commit;
    a raised exception here would roll back a legitimate mission transition
    for a purely diagnostic snapshot. Must swallow + log, not raise.
    """
    from csm.modules.workflow import orchestrator as orch_mod
    from csm.modules.workflow import orchestrator_state as _state

    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)
    orch = _make_orch(db)
    # Should not raise, should not create any file under tmp_path.
    await _state.write_state_yaml(orch, "does-not-exist-mission-id")
    assert not any(tmp_path.rglob("STATE.yaml"))


@pytest.mark.asyncio
async def test_delegators_forward_to_extracted_module(db, tmp_path, monkeypatch):
    """WorkflowOrchestrator._rescuer_loop / _startup_reap / _write_state_yaml
    forward to the extracted module fns.

    Uses monkeypatch to spy on the module-level fns; then calls the
    delegator methods and asserts the spies fired with `orch` as first arg.
    Also covers _finalize_mission_succeeded / _finalize_mission_failed.
    """
    from csm.modules.workflow import orchestrator as orch_mod
    from csm.modules.workflow import orchestrator_reaper as _reaper
    from csm.modules.workflow import orchestrator_state as _state

    monkeypatch.setattr(orch_mod.settings, "project_root", tmp_path)
    orch = _make_orch(db)

    reaper_loop_spy = AsyncMock()
    reaper_pass_spy = AsyncMock()
    reaper_reap_spy = AsyncMock()
    reaper_ok_spy = AsyncMock()
    reaper_fail_spy = AsyncMock()
    state_write_spy = AsyncMock()

    monkeypatch.setattr(_reaper, "rescuer_loop", reaper_loop_spy)
    monkeypatch.setattr(_reaper, "rescue_pass", reaper_pass_spy)
    monkeypatch.setattr(_reaper, "startup_reap", reaper_reap_spy)
    monkeypatch.setattr(_reaper, "finalize_mission_succeeded", reaper_ok_spy)
    monkeypatch.setattr(_reaper, "finalize_mission_failed", reaper_fail_spy)
    monkeypatch.setattr(_state, "write_state_yaml", state_write_spy)

    await orch._rescuer_loop()
    await orch._rescue_pass()
    await orch._startup_reap()
    await orch._finalize_mission_succeeded("m-1")
    await orch._finalize_mission_failed("m-2", "boom")
    await orch._write_state_yaml("m-3")

    reaper_loop_spy.assert_awaited_once_with(orch)
    reaper_pass_spy.assert_awaited_once_with(orch)
    reaper_reap_spy.assert_awaited_once_with(orch)
    reaper_ok_spy.assert_awaited_once_with(orch, "m-1")
    reaper_fail_spy.assert_awaited_once_with(orch, "m-2", "boom")
    state_write_spy.assert_awaited_once_with(orch, "m-3")

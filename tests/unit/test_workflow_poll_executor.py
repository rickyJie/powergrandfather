"""Tests for `PollExecutor` (M8 night2 T5).

Five required scenarios (per night_plan T5 brief):

1. `test_poll_passes_first_attempt_advances` — file already present so the
   first iteration's primitive passes → `on_pass` fires once, loop exits.
2. `test_poll_fails_then_passes_keeps_polling` — first iteration misses
   the file; a side-effect on the injected `sleep` creates it before the
   second iteration; `on_pass` fires on attempt 2.
3. `test_poll_exceeds_max_attempts_fails_mission` — file never appears,
   `max_attempts=3` → after 3 misses `on_fail` is called with a message
   that names the cap.
4. `test_cancel_poll_stops_loop` — sleep blocks indefinitely; calling
   `cancel_poll` cancels the task cleanly with neither callback firing.
5. `test_poll_shell_command_exit_zero_passes` — shell-exec form (`/bin/true`
   semantics) returns exit code 0 → `on_pass` fires.

All tests bypass the network/process boundaries that the live executor
crosses (no AUTO claude sessions, no actual `asyncio.sleep` wall time).
A real SQLite is still used so the executor's
`_load_render_context` path goes through the same ORM machinery as in
production.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock

import pytest
from csm.models import (
    Base,
    Mission,
    MissionStatus,
    WorkflowDefinition,
    WorkflowReviewStatus,
)
from csm.modules.workflow.engine import compile_workflow
from csm.modules.workflow.poll_executor import PollExecutor
from csm.modules.workflow.schema import StageSpec, load_workflow_spec
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


# Single poll stage targeting `<ws>/ready.txt`. `poll_interval` is 1s so the
# `_compute_max_attempts` derivation is meaningful in the timeout test, and
# tests can still drive sub-second behaviour via the injected `sleep` mock.
_POLL_YAML = """\
name: poll_wf
description: T5 poll executor fixture
parameters:
  - name: topic
    type: string
    required: true
stages:
  - name: wait_ready
    kind: poll
    poll_interval: 1s
    check:
      - file: "{ws}/ready.txt"
        primitives:
          - file_exists
"""

# Shell-exec form — `/bin/true` returns exit code 0. Used for the shell
# command test. Single check entry so we can be sure the pass came from
# the command, not from a co-passing primitive.
_POLL_SHELL_YAML = """\
name: poll_shell
description: T5 poll executor shell-exec fixture
parameters:
  - name: topic
    type: string
    required: true
stages:
  - name: wait_shell
    kind: poll
    poll_interval: 1s
    check:
      - command: ["/bin/true"]
"""

# Load-binding form + primitive that consumes the binding. Mirrors the
# sample_experiment `wait_train` shape: read a status JSON, extract a
# path, then check a file relative to that path exists.
_POLL_LOAD_YAML = """\
name: poll_load
description: post-fix load_as fixture
parameters:
  - name: topic
    type: string
    required: true
stages:
  - name: wait_train
    kind: poll
    poll_interval: 1s
    check:
      - file: "{ws}/status.json"
        load_as: json
        extract_field: metrics.exp_path
        as: exp_path
      - file: "{params.exp_path}/train_log/report.json"
        primitives:
          - file_exists
"""


async def _seed_workflow(sm, *, yaml_text: str) -> WorkflowDefinition:
    spec = load_workflow_spec(yaml_text)
    compiled = compile_workflow(spec)
    async with sm() as s:
        wf = WorkflowDefinition(
            name=spec.name,
            description=spec.description,
            file_path=f"/tmp/{spec.name}.workflow.yaml",
            yaml_content=yaml_text,
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
    current_stage: str,
    parameters: dict[str, Any] | None = None,
) -> Mission:
    async with sm() as s:
        m = Mission(
            workflow_def_id=wf.id,
            parameters=parameters or {"topic": "t"},
            workspace_path=workspace_path,
            status=MissionStatus.RUNNING,
            current_stage=current_stage,
        )
        s.add(m)
        await s.commit()
        await s.refresh(m)
        return m


def _stage_from(wf: WorkflowDefinition, stage_name: str) -> StageSpec:
    """Pull a StageSpec back out of the seeded workflow's YAML.

    The executor takes a `StageSpec` directly (not a name) so the tests
    mirror what the orchestrator does when handing a poll stage off.
    """
    spec = load_workflow_spec(wf.yaml_content)
    for s in spec.stages:
        if s.name == stage_name:
            return s
    raise AssertionError(f"test bug: stage {stage_name!r} not in fixture")


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_passes_first_attempt_advances(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ready.txt").write_text("ok")  # check passes on attempt 1

    wf = await _seed_workflow(db, yaml_text=_POLL_YAML)
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_ready"
    )
    stage = _stage_from(wf, "wait_ready")

    on_pass = AsyncMock()
    on_fail = AsyncMock()
    sleep = AsyncMock()
    pe = PollExecutor(
        db, on_pass=on_pass, on_fail=on_fail, sleep=sleep, default_max_attempts=5
    )

    task = await pe.start_poll(mission.id, "wait_ready", stage)
    await task

    on_pass.assert_awaited_once_with(mission.id, "wait_ready")
    on_fail.assert_not_awaited()
    # Sleep is only called between attempts — first-attempt pass means no sleep.
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_fails_then_passes_keeps_polling(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # File does NOT exist yet — first attempt must fail.

    wf = await _seed_workflow(db, yaml_text=_POLL_YAML)
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_ready"
    )
    stage = _stage_from(wf, "wait_ready")

    # Side-effect: create the file the first time sleep is awaited so the
    # second iteration's check passes. Counts sleep calls so we can assert
    # the loop did not short-circuit.
    sleep_calls: list[float] = []

    async def sleep_side_effect(interval: float) -> None:
        sleep_calls.append(interval)
        # On the first inter-attempt sleep, materialise the target file.
        if len(sleep_calls) == 1:
            (ws / "ready.txt").write_text("now ready")

    on_pass = AsyncMock()
    on_fail = AsyncMock()
    pe = PollExecutor(
        db,
        on_pass=on_pass,
        on_fail=on_fail,
        sleep=sleep_side_effect,
        default_max_attempts=5,
    )

    task = await pe.start_poll(mission.id, "wait_ready", stage)
    await task

    on_pass.assert_awaited_once_with(mission.id, "wait_ready")
    on_fail.assert_not_awaited()
    # Exactly one sleep — between attempt 1 (fail) and attempt 2 (pass).
    assert sleep_calls == [1.0], (
        f"expected one inter-attempt sleep at interval=1s, got {sleep_calls}"
    )


@pytest.mark.asyncio
async def test_poll_exceeds_max_attempts_fails_mission(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # File never created — every attempt misses.

    wf = await _seed_workflow(db, yaml_text=_POLL_YAML)
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_ready"
    )
    stage = _stage_from(wf, "wait_ready")

    sleep = AsyncMock()
    on_pass = AsyncMock()
    on_fail = AsyncMock()
    pe = PollExecutor(
        db,
        on_pass=on_pass,
        on_fail=on_fail,
        sleep=sleep,
        default_max_attempts=3,
    )

    task = await pe.start_poll(mission.id, "wait_ready", stage)
    await task

    on_pass.assert_not_awaited()
    on_fail.assert_awaited_once()
    args, _kwargs = on_fail.await_args
    sent_mid, sent_stage, sent_reason = args
    assert sent_mid == mission.id
    assert sent_stage == "wait_ready"
    assert "max_attempts=3" in sent_reason
    # Sleep called between attempts 1→2 and 2→3 — NOT after attempt 3 (we
    # bail before sleeping when max is reached). So exactly 2 awaits.
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_cancel_poll_stops_loop(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # File missing → loop would run forever if not cancelled.

    wf = await _seed_workflow(db, yaml_text=_POLL_YAML)
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_ready"
    )
    stage = _stage_from(wf, "wait_ready")

    # Sleep blocks until cancelled; gives us a deterministic "loop is
    # parked between attempts" state for the cancel call to interrupt.
    sleep_event = asyncio.Event()

    async def blocking_sleep(_interval: float) -> None:
        await sleep_event.wait()

    on_pass = AsyncMock()
    on_fail = AsyncMock()
    pe = PollExecutor(
        db,
        on_pass=on_pass,
        on_fail=on_fail,
        sleep=blocking_sleep,
        default_max_attempts=1_000_000,
    )

    task = await pe.start_poll(mission.id, "wait_ready", stage)
    # Yield once so the loop's first iteration runs and parks in sleep.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert ("wait_ready" in [k[1] for k in pe._tasks.keys()]) or task.done(), (
        "task should be tracked while running"
    )

    await pe.cancel_poll(mission.id, "wait_ready")

    assert task.done()
    assert task.cancelled() or task.exception() is None
    on_pass.assert_not_awaited()
    on_fail.assert_not_awaited()
    # _tasks dict cleaned up after cancellation.
    assert (mission.id, "wait_ready") not in pe._tasks


@pytest.mark.asyncio
async def test_poll_load_binding_extracts_and_persists(db, tmp_path):
    """load_as: json + extract_field + as → bound value drives next check.

    Regression against the T5-era "no-op load-binding" branch that
    dead-ended the sample_experiment workflow: `{exp_path}` never
    resolved because nobody wrote to `mission.parameters`.
    """
    import json as _json

    from csm.models import Mission

    ws = tmp_path / "ws"
    ws.mkdir()
    exp_dir = tmp_path / "exp-1234"
    (exp_dir / "train_log").mkdir(parents=True)
    (exp_dir / "train_log" / "report.json").write_text("{}")
    (ws / "status.json").write_text(
        _json.dumps({"metrics": {"exp_path": str(exp_dir)}})
    )

    wf = await _seed_workflow(db, yaml_text=_POLL_LOAD_YAML)
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_train"
    )
    stage = _stage_from(wf, "wait_train")

    on_pass = AsyncMock()
    on_fail = AsyncMock()
    sleep = AsyncMock()
    pe = PollExecutor(
        db, on_pass=on_pass, on_fail=on_fail, sleep=sleep, default_max_attempts=3
    )

    task = await pe.start_poll(mission.id, "wait_train", stage)
    await task

    on_pass.assert_awaited_once_with(mission.id, "wait_train")
    on_fail.assert_not_awaited()
    # Binding was persisted to mission.parameters — downstream stages see it.
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert m.parameters.get("exp_path") == str(exp_dir)


@pytest.mark.asyncio
async def test_poll_load_binding_fails_when_field_missing(db, tmp_path):
    """When the JSON path resolves to nothing, the iteration fails.

    Fail mode keeps the loop going (retry) rather than terminating —
    the binding might appear in a later iteration once the upstream
    process writes the field.
    """
    import json as _json

    from csm.models import Mission

    ws = tmp_path / "ws"
    ws.mkdir()
    # Status JSON exists but is missing the `metrics.exp_path` field.
    (ws / "status.json").write_text(_json.dumps({"metrics": {"other": "x"}}))

    wf = await _seed_workflow(db, yaml_text=_POLL_LOAD_YAML)
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_train"
    )
    stage = _stage_from(wf, "wait_train")

    sleep = AsyncMock()
    on_pass = AsyncMock()
    on_fail = AsyncMock()
    pe = PollExecutor(
        db, on_pass=on_pass, on_fail=on_fail, sleep=sleep, default_max_attempts=2
    )

    task = await pe.start_poll(mission.id, "wait_train", stage)
    await task

    on_pass.assert_not_awaited()
    on_fail.assert_awaited_once()
    args, _ = on_fail.await_args
    assert "metrics.exp_path" in args[2]
    # Nothing bound on failure.
    async with db() as s:
        m = await s.get(Mission, mission.id)
        assert "exp_path" not in (m.parameters or {})


@pytest.mark.asyncio
async def test_poll_shell_command_exit_zero_passes(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    wf = await _seed_workflow(db, yaml_text=_POLL_SHELL_YAML)
    mission = await _seed_mission(
        db, wf, workspace_path=str(ws), current_stage="wait_shell"
    )
    stage = _stage_from(wf, "wait_shell")

    on_pass = AsyncMock()
    on_fail = AsyncMock()
    sleep = AsyncMock()
    pe = PollExecutor(
        db, on_pass=on_pass, on_fail=on_fail, sleep=sleep, default_max_attempts=3
    )

    task = await pe.start_poll(mission.id, "wait_shell", stage)
    await task

    on_pass.assert_awaited_once_with(mission.id, "wait_shell")
    on_fail.assert_not_awaited()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_shell_check_timeout_kills_and_reaps_process(monkeypatch):
    """A stuck external check returns a failed result instead of hanging."""
    from csm.config import settings

    monkeypatch.setattr(settings, "workflow_shell_check_timeout_sec", 0.01)
    passed, reason = await PollExecutor._run_shell_check(["/bin/sleep", "10"])

    assert passed is False
    assert "timed out after" in reason

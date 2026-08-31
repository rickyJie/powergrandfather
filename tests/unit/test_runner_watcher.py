"""Unit tests for AutomationRunner's completion-detection path.

Primary signal is `MESSAGE_ASSISTANT_DONE` → grace timer → stop_session,
re-armed on every assistant turn within the grace window. A 10-minute hard
timeout is a backstop for the case where end_turn never arrives.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from csm.core.events import Event, EventType
from csm.modules.automation.runner import AutomationRunner


class _FakeMgr:
    """Records stop_session() calls so tests can verify runner behavior."""

    def __init__(self) -> None:
        self.stopped: list[tuple[str, bool]] = []

    async def stop_session(self, sid: str, graceful: bool = True) -> int | None:
        self.stopped.append((sid, graceful))
        return 0


def _make_runner(mgr: _FakeMgr, grace_sec: float = 0.1) -> AutomationRunner:
    # The completion path only uses self._mgr.stop_session + the in-memory
    # maps; stub the rest to avoid needing a real EventStream / sessionmaker.
    r = AutomationRunner(
        sessionmaker=None,  # type: ignore[arg-type]
        event_stream=None,  # type: ignore[arg-type]
        session_manager=mgr,
        grace_sec=grace_sec,
    )
    return r


def _register(runner: AutomationRunner, csm_sid: str, external_sid: str, run_id: str) -> None:
    runner._runs_by_csm_session[csm_sid] = run_id
    runner._csm_by_external_session[external_sid] = csm_sid


def _assistant_done(external_sid: str) -> Event:
    return Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.utcnow(),
        session_id=external_sid,
        project_path=None,
        payload={"model": "claude-opus-4-7"},
    )


# ---------- happy path: end_turn → grace → stop ----------

@pytest.mark.asyncio
async def test_assistant_done_triggers_grace_then_stop() -> None:
    mgr = _FakeMgr()
    runner = _make_runner(mgr, grace_sec=0.05)
    _register(runner, csm_sid="sess-1", external_sid="claude-1", run_id="run-1")

    await runner._on_assistant_done(_assistant_done("claude-1"))
    assert "run-1" in runner._grace_tasks  # timer armed

    # Wait past the grace window.
    await asyncio.sleep(0.15)
    assert mgr.stopped == [("sess-1", True)]
    assert "run-1" not in runner._grace_tasks  # cleaned up after firing


# ---------- multi-turn: subsequent end_turn re-arms, doesn't fire early ----------

@pytest.mark.asyncio
async def test_grace_re_armed_by_followup_assistant_done() -> None:
    mgr = _FakeMgr()
    # 0.2s grace so we have headroom to fire two events inside one window.
    runner = _make_runner(mgr, grace_sec=0.2)
    _register(runner, csm_sid="sess-2", external_sid="claude-2", run_id="run-2")

    await runner._on_assistant_done(_assistant_done("claude-2"))
    first_task = runner._grace_tasks["run-2"]

    # Wait less than grace, then fire another end_turn.
    await asyncio.sleep(0.1)
    assert mgr.stopped == []  # nothing fired yet
    await runner._on_assistant_done(_assistant_done("claude-2"))
    # Original timer should be cancelled; a new one should be in place.
    await asyncio.sleep(0)  # let the cancellation propagate
    assert first_task.cancelled() or first_task.done()
    assert runner._grace_tasks["run-2"] is not first_task

    # Still no stop yet (we just re-armed).
    await asyncio.sleep(0.1)
    assert mgr.stopped == []

    # After the new full grace window elapses, stop should fire.
    await asyncio.sleep(0.2)
    assert mgr.stopped == [("sess-2", True)]


# ---------- terminal event cancels pending grace ----------

@pytest.mark.asyncio
async def test_terminal_cancels_pending_grace() -> None:
    """If SESSION_ENDED arrives during the grace window, the timer must be
    cancelled so it doesn't fire a redundant stop_session after finalization.
    """
    mgr = _FakeMgr()
    runner = _make_runner(mgr, grace_sec=0.2)
    _register(runner, csm_sid="sess-3", external_sid="claude-3", run_id="run-3")

    await runner._on_assistant_done(_assistant_done("claude-3"))
    grace_task = runner._grace_tasks["run-3"]

    # Simulate the cleanup branch of _on_session_terminal (the parts we can
    # exercise without a real DB): pop the run mapping, cancel the grace task,
    # clear the claude-side mapping.
    runner._runs_by_csm_session.pop("sess-3", None)
    t = runner._grace_tasks.pop("run-3", None)
    if t is not None and not t.done():
        t.cancel()
    runner._csm_by_external_session.pop("claude-3", None)

    # Wait past what would have been the grace window.
    await asyncio.sleep(0.3)
    assert mgr.stopped == []  # grace never fired
    assert grace_task.cancelled()


# ---------- guard: assistant_done for an unrelated claude session is ignored ----------

@pytest.mark.asyncio
async def test_assistant_done_ignored_for_unknown_session() -> None:
    mgr = _FakeMgr()
    runner = _make_runner(mgr, grace_sec=0.05)
    # No registrations.

    await runner._on_assistant_done(_assistant_done("claude-unknown"))
    assert runner._grace_tasks == {}

    # Even an event with no session_id at all must be a safe no-op.
    bare = Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.utcnow(),
        session_id=None,
        project_path=None,
        payload={},
    )
    await runner._on_assistant_done(bare)
    await asyncio.sleep(0.1)
    assert mgr.stopped == []


# ---------- stop(): cancels any pending grace timers cleanly ----------

@pytest.mark.asyncio
async def test_stop_cancels_pending_grace_timers() -> None:
    mgr = _FakeMgr()
    runner = _make_runner(mgr, grace_sec=5.0)  # long enough that it won't fire
    # Bypass start() (it would need a real EventStream); just simulate two
    # in-flight grace timers and ensure stop() cleans them up.
    _register(runner, csm_sid="sess-6a", external_sid="claude-6a", run_id="run-6a")
    _register(runner, csm_sid="sess-6b", external_sid="claude-6b", run_id="run-6b")
    await runner._on_assistant_done(_assistant_done("claude-6a"))
    await runner._on_assistant_done(_assistant_done("claude-6b"))
    tasks = list(runner._grace_tasks.values())
    assert len(tasks) == 2

    await runner.stop()
    # Give the cancellations a tick to propagate.
    await asyncio.sleep(0)
    assert runner._grace_tasks == {}
    assert all(t.cancelled() or t.done() for t in tasks)
    assert mgr.stopped == []  # nothing should have fired


# ---------- G6 idle backstop: SESSION_IDLE forces stop_session ----------


def _session_idle(external_sid: str, idle_seconds: int) -> Event:
    return Event(
        type=EventType.SESSION_IDLE,
        ts=datetime.utcnow(),
        session_id=external_sid,
        project_path=None,
        payload={"idle_seconds": idle_seconds},
    )


@pytest.mark.asyncio
async def test_idle_event_force_stops_auto_session() -> None:
    """G6 · SESSION_IDLE on a tracked AUTO session → immediate stop_session.

    Reproduces the Phase 1 finding where an AUTO claude session hung 3h
    in an interactive REPL (no `end_turn` ever emitted, grace timer
    unarmed). EventStream's watchdog fires SESSION_IDLE for that
    session; the runner must treat it as terminal.
    """
    mgr = _FakeMgr()
    runner = _make_runner(mgr, grace_sec=5.0)
    _register(runner, csm_sid="sess-idle", external_sid="claude-idle", run_id="run-idle")

    await runner._on_session_idle(_session_idle("claude-idle", idle_seconds=1900))

    assert mgr.stopped == [("sess-idle", True)]


@pytest.mark.asyncio
async def test_idle_event_ignored_for_untracked_session() -> None:
    """SESSION_IDLE for a claude session we never tracked → no-op.

    Interactive user sessions emit SESSION_IDLE too; only AUTO Runs go
    into `_csm_by_external_session`, so the runner should skip anything
    it doesn't recognise.
    """
    mgr = _FakeMgr()
    runner = _make_runner(mgr, grace_sec=5.0)
    # NOTE: intentionally no _register — this external_sid is unknown.

    await runner._on_session_idle(_session_idle("claude-interactive", idle_seconds=1900))

    assert mgr.stopped == []


@pytest.mark.asyncio
async def test_idle_event_cancels_pending_grace_before_stopping() -> None:
    """If a grace timer is armed AND idle fires, we cancel the grace and stop now.

    Avoids double-stop and the race where the grace timer might fire
    against a session that's already being torn down.
    """
    mgr = _FakeMgr()
    runner = _make_runner(mgr, grace_sec=5.0)  # long grace so it won't fire on its own
    _register(runner, csm_sid="sess-both", external_sid="claude-both", run_id="run-both")

    # Arm a grace timer, then fire idle before grace expires.
    await runner._on_assistant_done(_assistant_done("claude-both"))
    assert "run-both" in runner._grace_tasks
    grace_task = runner._grace_tasks["run-both"]

    await runner._on_session_idle(_session_idle("claude-both", idle_seconds=1900))

    # Grace was cancelled and idle-driven stop_session fired exactly once.
    await asyncio.sleep(0)  # let cancellation propagate
    assert grace_task.cancelled() or grace_task.done()
    assert mgr.stopped == [("sess-both", True)]

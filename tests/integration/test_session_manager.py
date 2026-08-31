"""Session Manager integration tests using `bash` instead of `claude`.

The PTY plumbing, ring buffer, stop/kill semantics and DB row updates can all
be exercised with a vanilla shell — we don't need a real Claude binary to
verify the manager contracts.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from csm.backends import build_default_registry
from csm.core.event_stream import EventStream
from csm.core.events import EventType
from csm.models import Base, Session
from csm.models.session import SessionStatus, SessionType
from csm.modules.session_manager.manager import SessionManager, _pid_alive
from csm.modules.session_manager.ring_buffer import RingBuffer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


# ---- ring buffer ----
def test_ring_buffer_basic():
    rb = RingBuffer(capacity=10)
    rb.write(b"abcdef")
    assert rb.snapshot() == b"abcdef"
    rb.write(b"ghij")
    assert rb.snapshot() == b"abcdefghij"
    rb.write(b"k")
    snap = rb.snapshot()
    assert len(snap) <= 10
    assert snap.endswith(b"k")


def test_ring_buffer_overflow_single_payload():
    rb = RingBuffer(capacity=5)
    rb.write(b"abcdefghij")
    assert rb.snapshot() == b"fghij"


# ---- edge cases (added in deepen round) ----
def test_ring_buffer_exact_capacity_boundary():
    """Writing exactly capacity bytes should keep everything; one more byte evicts head."""
    rb = RingBuffer(capacity=10)
    rb.write(b"0123456789")
    assert rb.snapshot() == b"0123456789"
    assert rb.size() == 10
    rb.write(b"a")
    snap = rb.snapshot()
    assert len(snap) <= 10
    assert snap.endswith(b"a")
    assert b"0" not in snap[:1]


def test_ring_buffer_empty_write_is_noop():
    rb = RingBuffer(capacity=10)
    rb.write(b"")
    assert rb.snapshot() == b""
    assert rb.size() == 0


def test_ring_buffer_clear():
    rb = RingBuffer(capacity=10)
    rb.write(b"abc")
    rb.clear()
    assert rb.snapshot() == b""
    assert rb.size() == 0


def test_pid_probe_treats_linux_zombie_as_dead(monkeypatch):
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda _self, **_kwargs: "123 (finished command) Z 1 2 3",
    )
    kill_called = False

    def _kill(_pid, _signal):
        nonlocal kill_called
        kill_called = True

    monkeypatch.setattr(os, "kill", _kill)
    assert _pid_alive(123) is False
    assert kill_called is False


# ---- session manager ----
@pytest.fixture
async def setup(tmp_path, monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp_proj = tempfile.mkdtemp()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    from csm.config import settings
    monkeypatch.setattr(settings, "session_output_dir", tmp_path / "session-output")

    # EventStream with no projects dir (won't find anything).
    es = EventStream(projects_root=Path(tmp_proj), poll_interval_sec=10.0, watchdog_interval_sec=10.0)

    mgr = SessionManager(
        sessionmaker=sessionmaker,
        event_stream=es,
        adapter_registry=build_default_registry(),
        ring_buffer_bytes=4096,
        stop_grace_sec=1,
        claude_argv=["bash", "-i"],  # interactive bash as a stand-in
    )

    yield mgr, sessionmaker

    await mgr.shutdown()
    await es.stop()
    await engine.dispose()
    os.unlink(db_path)


async def test_spawn_and_stop(setup):
    mgr, sm = setup
    sess = await mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE)
    assert sess is not None
    # Post-spawn initial state is IDLE — process alive, agent not
    # working yet. UserPromptSubmit/PreToolUse promote to RUNNING.
    assert sess.status == SessionStatus.IDLE
    assert sess.pid > 0

    # Send a tiny command so bash produces output.
    await mgr.write_input(sess.id, b"echo hello_csm\n")
    # Give the reader loop a moment.
    await asyncio.sleep(0.4)

    # Ring buffer should have captured the echo output.
    live = mgr._live[sess.id]
    snap = live.ring.snapshot()
    assert b"hello_csm" in snap

    # Stop gracefully.
    await mgr.stop_session(sess.id, graceful=True)
    # Wait for finalizer to flip status.
    for _ in range(20):
        row = await mgr.get_session(sess.id)
        if row.status in (SessionStatus.EXITED, SessionStatus.CRASHED):
            break
        await asyncio.sleep(0.05)
    row = await mgr.get_session(sess.id)
    assert row.status in (SessionStatus.EXITED, SessionStatus.CRASHED)
    assert row.ended_at is not None
    saved, source = await mgr.output_snapshot(sess.id)
    assert source == "persisted"
    assert b"hello_csm" in saved


async def test_list_sessions(setup):
    mgr, _ = setup
    a = await mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE, title="A")
    b = await mgr.create_session(cwd="/tmp", type=SessionType.AUTO, title="B")
    rows = await mgr.list_sessions()
    ids = {r.id for r in rows}
    assert a.id in ids and b.id in ids
    autos = await mgr.list_sessions(type_in=[SessionType.AUTO])
    assert all(r.type == SessionType.AUTO for r in autos)


async def test_list_sessions_offset_and_exact_count(setup):
    mgr, _ = setup
    for title in ("one", "two", "three"):
        await mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE, title=title)
    first = await mgr.list_sessions(type_in=[SessionType.INTERACTIVE], limit=2, offset=0)
    second = await mgr.list_sessions(type_in=[SessionType.INTERACTIVE], limit=2, offset=2)
    assert len(first) == 2
    assert len(second) == 1
    assert not ({row.id for row in first} & {row.id for row in second})
    assert await mgr.count_sessions(type_in=[SessionType.INTERACTIVE]) == 3


async def test_kill_session(setup):
    mgr, _ = setup
    sess = await mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE)
    await mgr.kill_session(sess.id)
    for _ in range(20):
        row = await mgr.get_session(sess.id)
        if row.status in (SessionStatus.EXITED, SessionStatus.CRASHED):
            break
        await asyncio.sleep(0.05)
    row = await mgr.get_session(sess.id)
    assert row.status in (SessionStatus.EXITED, SessionStatus.CRASHED)


async def test_write_input_nonexistent_session_returns_zero(setup):
    """Writing input to a session id that was never created should not crash; returns 0 bytes written."""
    mgr, _ = setup
    n = await mgr.write_input("does-not-exist-xxx", b"hello")
    assert n == 0


async def test_stop_nonexistent_session_returns_none(setup):
    """Stopping a session id that was never created should not crash; returns None."""
    mgr, _ = setup
    code = await mgr.stop_session("ghost-session-id", graceful=True)
    assert code is None


async def test_list_sessions_filter_combinations(setup):
    """Combined status_in + type_in filters narrow correctly."""
    mgr, _ = setup
    a = await mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE, title="A")
    b = await mgr.create_session(cwd="/tmp", type=SessionType.AUTO, title="B")
    await mgr.stop_session(b.id, graceful=False)
    # Give finalize a moment.
    await asyncio.sleep(0.3)

    # Post-spawn initial state is IDLE (not RUNNING). Filter on the
    # "live agent" set: IDLE + RUNNING + WAITING_INPUT.
    live_states = [SessionStatus.RUNNING, SessionStatus.IDLE, SessionStatus.WAITING_INPUT]
    live_only = await mgr.list_sessions(status_in=live_states)
    live_ids = {r.id for r in live_only}
    # a should still be alive; b should be exited/crashed.
    assert a.id in live_ids
    assert b.id not in live_ids

    auto_live = await mgr.list_sessions(
        status_in=live_states, type_in=[SessionType.AUTO]
    )
    assert all(r.type == SessionType.AUTO and r.status in live_states for r in auto_live)


async def test_parallel_session_creation(setup):
    """Three concurrent create_session calls should all succeed with distinct PIDs."""
    mgr, _ = setup
    sessions = await asyncio.gather(
        mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE),
        mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE),
        mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE),
    )
    assert len({s.id for s in sessions}) == 3
    assert len({s.pid for s in sessions}) == 3
    # Post-spawn initial state is IDLE, not RUNNING.
    assert all(s.status == SessionStatus.IDLE for s in sessions)


async def test_orphan_reap_on_startup(setup):
    mgr, sm = setup
    # Manually insert a row with a fake dead pid.
    from csm.models import Session as SessRow
    async with sm() as db:
        fake = SessRow(cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, pid=1)  # pid=1 (init), but checking via os.kill(pid, 0)
        # Use a guaranteed-dead pid: a very high number unlikely to be used.
        fake.pid = 999999
        db.add(fake)
        await db.commit()
        fake_id = fake.id
    reaped = await mgr.startup_reap_orphans()
    assert reaped >= 1
    row = await mgr.get_session(fake_id)
    assert row.status in (SessionStatus.CRASHED, SessionStatus.ORPHANED)


async def test_reap_covers_waiting_and_orphaned_statuses(setup):
    """Regression: a session parked in waiting_input / waiting_auth /
    orphaned when the backend died must also be reaped on restart —
    otherwise the frontend keeps showing it in the Active tab even
    though `self._live` is empty on the fresh process."""
    mgr, sm = setup
    from csm.models import Session as SessRow
    async with sm() as db:
        w_in = SessRow(cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.WAITING_INPUT, pid=999997)
        w_au = SessRow(cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.WAITING_AUTH, pid=999998)
        orph = SessRow(cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.ORPHANED, pid=999999)
        db.add_all([w_in, w_au, orph])
        await db.commit()
        ids = (w_in.id, w_au.id, orph.id)
    reaped = await mgr.startup_reap_orphans()
    assert reaped >= 3
    for sid in ids:
        row = await mgr.get_session(sid)
        assert row.status in (SessionStatus.CRASHED, SessionStatus.ORPHANED)


# ---------------------------------------------------------------------------
# Periodic orphan reaper — 2026-07-26 UI-stuck-on-orphaned regression
# ---------------------------------------------------------------------------


async def test_periodic_orphan_reap_dead_pid(setup):
    """`startup_reap_orphans` runs only on boot. If a row was marked
    ORPHANED at that moment (pid was alive), and the pid later dies
    mid-uptime, the row stays ORPHANED forever without this periodic
    partner — user sees the "CSM lost the handle" banner even though
    the process is long gone."""
    from csm.models import Session as SessRow
    mgr, sm = setup
    async with sm() as db:
        row = SessRow(cwd="/tmp", type=SessionType.INTERACTIVE,
                      status=SessionStatus.ORPHANED, pid=999999)
        db.add(row)
        await db.commit()
        sid = row.id
    # One reap tick — call the private loop's body directly rather than
    # sleeping through the 30s interval.
    await mgr._orphan_reap_tick()  # type: ignore[attr-defined]
    row = await mgr.get_session(sid)
    assert row.status == SessionStatus.CRASHED
    assert row.ended_at is not None


async def test_periodic_orphan_reap_leaves_alive_pid_alone(setup):
    """An orphaned row whose pid is still alive (e.g. genuinely
    reparented / driven from tmux) must NOT be reaped — that's exactly
    the conservative case the ORPHANED status exists for."""
    from csm.models import Session as SessRow
    mgr, sm = setup
    async with sm() as db:
        row = SessRow(cwd="/tmp", type=SessionType.INTERACTIVE,
                      status=SessionStatus.ORPHANED, pid=os.getpid())
        db.add(row)
        await db.commit()
        sid = row.id
    await mgr._orphan_reap_tick()  # type: ignore[attr-defined]
    row = await mgr.get_session(sid)
    assert row.status == SessionStatus.ORPHANED
    assert row.ended_at is None


async def test_periodic_orphan_reap_ignores_non_orphaned(setup):
    """Sanity: exited/crashed/running rows are not touched by the reaper —
    scope is orphaned-only."""
    from csm.models import Session as SessRow
    mgr, sm = setup
    async with sm() as db:
        exited = SessRow(cwd="/tmp", type=SessionType.INTERACTIVE,
                         status=SessionStatus.EXITED, pid=999999)
        crashed = SessRow(cwd="/tmp", type=SessionType.INTERACTIVE,
                          status=SessionStatus.CRASHED, pid=999998)
        db.add_all([exited, crashed])
        await db.commit()
        ids = (exited.id, crashed.id)
    await mgr._orphan_reap_tick()  # type: ignore[attr-defined]
    exited_row = await mgr.get_session(ids[0])
    crashed_row = await mgr.get_session(ids[1])
    assert exited_row.status == SessionStatus.EXITED
    assert crashed_row.status == SessionStatus.CRASHED


# ---- Ctrl-C / interrupt releases stuck "agent working" ----


async def _set_status(sm, sid, status):
    async with sm() as db:
        row = await db.get(Session, sid)
        row.status = status
        row.current_tool = "Bash: sleep 999"
        await db.commit()


async def test_interrupt_running_session_goes_idle(setup):
    """A RUNNING session (agent working) must fall back to IDLE on interrupt,
    clear current_tool, and emit SESSION_INTERRUPTED — the Ctrl-C fix for the
    'stuck at agent working after Ctrl-C' bug (no Stop hook fires on abort)."""
    mgr, sm = setup
    sess = await mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE)
    await _set_status(sm, sess.id, SessionStatus.RUNNING)

    seen: list = []

    async def _collect(e):
        seen.append(e)

    sub = mgr._es.subscribe([EventType.SESSION_INTERRUPTED], _collect)
    try:
        await mgr._on_interrupt(sess.id)
    finally:
        mgr._es.unsubscribe(sub)

    async with sm() as db:
        row = await db.get(Session, sess.id)
        assert row.status == SessionStatus.IDLE
        assert row.current_tool is None
    assert len(seen) == 1
    assert seen[0].payload["csm_session_id"] == sess.id


async def test_interrupt_noop_when_not_running(setup):
    """Ctrl-C while the session is already IDLE (nothing in flight) must not
    emit an event or change state — the guard keeps repeated Ctrl-C cheap."""
    mgr, sm = setup
    sess = await mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE)
    # Fresh spawn is IDLE.
    seen: list = []

    async def _collect(e):
        seen.append(e)

    sub = mgr._es.subscribe([EventType.SESSION_INTERRUPTED], _collect)
    try:
        await mgr._on_interrupt(sess.id)
    finally:
        mgr._es.unsubscribe(sub)

    async with sm() as db:
        row = await db.get(Session, sess.id)
        assert row.status == SessionStatus.IDLE
    assert seen == []


async def test_ctrl_c_byte_via_write_input_releases_running(setup):
    """Writing a raw ETX (0x03) through write_input — the API interrupt path —
    schedules the release. Covers the b'\\x03' detection end-to-end."""
    mgr, sm = setup
    sess = await mgr.create_session(cwd="/tmp", type=SessionType.INTERACTIVE)
    await _set_status(sm, sess.id, SessionStatus.RUNNING)

    await mgr.write_input(sess.id, b"\x03")
    # _note_interrupt_bytes fires a background task; let it run.
    await asyncio.sleep(0.1)

    async with sm() as db:
        row = await db.get(Session, sess.id)
        assert row.status == SessionStatus.IDLE

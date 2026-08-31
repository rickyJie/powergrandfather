"""Batch per-tick adapter projection (backend-review W1).

`_project_adapter_events` must project ALL of a tick's events in ONE
transaction: last-write-wins final state, `csm_session_id` stamped on each
projected event only AFTER the commit is durable, unknown rows skipped, and
non-projectable event types left alone. Previously each event opened its own
session + commit, so a burst of N events was N serialized SQLite writes that
monopolized the single WAL writer and starved interactive writers.
"""
from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import Base, Session
from csm.models.session import SessionStatus, SessionType
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def setup():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    es = EventStream(
        projects_root=Path(tempfile.gettempdir()),
        poll_interval_sec=10.0,
        watchdog_interval_sec=10.0,
        sessionmaker=sm,
    )
    yield es, sm
    await es.stop()
    await engine.dispose()
    os.unlink(db_path)


async def _plant(sm, sid, ext, status=SessionStatus.RUNNING, agent="claude"):
    async with sm() as db:
        db.add(Session(
            id=sid, cwd="/tmp", type=SessionType.INTERACTIVE,
            status=status, external_session_id=ext, agent=agent,
        ))
        await db.commit()


def _ev(t, ext, agent="claude"):
    return Event(
        type=t, ts=datetime.now(UTC), session_id=ext,
        project_path=None, payload={"agent": agent},
    )


async def _get(sm, sid):
    async with sm() as db:
        return await db.get(Session, sid)


async def test_batch_last_write_wins_and_stamps_events(setup):
    """USER_SENT then ASSISTANT_DONE for one session in a single tick → the
    final committed status is IDLE, and every projected event carries the
    resolved csm_session_id (stamped only after the batch commit)."""
    es, sm = setup
    await _plant(sm, "s1", "ext-1", status=SessionStatus.RUNNING)
    events = [
        _ev(EventType.MESSAGE_USER_SENT, "ext-1"),
        _ev(EventType.MESSAGE_ASSISTANT_DONE, "ext-1"),
    ]
    await es._project_adapter_events(events, "claude")
    row = await _get(sm, "s1")
    assert row.status == SessionStatus.IDLE
    assert all(e.payload.get("csm_session_id") == "s1" for e in events)


async def test_batch_projects_multiple_sessions_in_one_tick(setup):
    """Two sessions' events in one tick both land correctly from one commit."""
    es, sm = setup
    await _plant(sm, "s1", "ext-1", status=SessionStatus.RUNNING)
    await _plant(sm, "s2", "ext-2", status=SessionStatus.STARTING)
    events = [
        _ev(EventType.MESSAGE_ASSISTANT_DONE, "ext-1"),
        _ev(EventType.MESSAGE_USER_SENT, "ext-2"),
    ]
    await es._project_adapter_events(events, "claude")
    assert (await _get(sm, "s1")).status == SessionStatus.IDLE
    assert (await _get(sm, "s2")).status == SessionStatus.RUNNING


async def test_batch_skips_unknown_row_but_projects_others(setup):
    """An event whose external id matches no live row is skipped (no stamp),
    while the rest of the tick still commits."""
    es, sm = setup
    await _plant(sm, "s1", "ext-1", status=SessionStatus.RUNNING)
    events = [
        _ev(EventType.MESSAGE_ASSISTANT_DONE, "ext-does-not-exist"),
        _ev(EventType.MESSAGE_ASSISTANT_DONE, "ext-1"),
    ]
    await es._project_adapter_events(events, "claude")
    assert (await _get(sm, "s1")).status == SessionStatus.IDLE
    assert events[0].payload.get("csm_session_id") is None
    assert events[1].payload.get("csm_session_id") == "s1"


async def test_batch_ignores_non_projectable_events(setup):
    """A non-lifecycle event type (e.g. SESSION_CRASHED) is not projected here
    and must not mutate the row status."""
    es, sm = setup
    await _plant(sm, "s1", "ext-1", status=SessionStatus.RUNNING)
    await es._project_adapter_events(
        [_ev(EventType.SESSION_CRASHED, "ext-1")], "claude"
    )
    assert (await _get(sm, "s1")).status == SessionStatus.RUNNING

"""Edge cases for the worktime subsystem.

Covers:
- Tracker normal open/close on USER_SENT → ASSISTANT_DONE.
- Tracker coalesce on repeated USER_SENT with no ASSISTANT_DONE in between.
- Tracker terminal-event close on SESSION_ENDED without a matching DONE.
- Tracker 30-min safety cap when ASSISTANT_DONE arrives after a huge gap.
- Heartbeat manager: open → extend within grace → close via sweeper.
- WorktimeService.reap_orphans_on_boot closes any dangling interval.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from csm.core.events import Event, EventType
from csm.models import (
    Base,
    Session,
    WorkInterval,
    WorkIntervalKind,
    WorkIntervalSource,
)
from csm.models.session import SessionStatus, SessionType
from csm.modules.worktime.heartbeat import HeartbeatManager
from csm.modules.worktime.service import WorktimeService
from csm.modules.worktime.tracker import WorktimeTracker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


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


def _make_event(kind: EventType, sid: str, ts: datetime) -> Event:
    return Event(type=kind, ts=ts, session_id=sid, project_path=None)


def _fake_event_stream() -> MagicMock:
    """Minimal EventStream stand-in for tests — just enough for subscribe/unsubscribe."""
    m = MagicMock()
    m.subscribe = MagicMock(return_value="sub-id")
    m.unsubscribe = MagicMock()
    return m


async def _seed_session(db, external_sid: str) -> str:
    async with db() as s:
        row = Session(
            cwd="/tmp/p",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            external_session_id=external_sid,
        )
        s.add(row)
        await s.commit()
        return row.id


# ---- Tracker ------------------------------------------------------------


async def test_tracker_normal_open_close(db):
    csm_id = await _seed_session(db, "ext-1")
    tracker = WorktimeTracker(sessionmaker=db, event_stream=_fake_event_stream())
    t0 = datetime(2026, 8, 4, 10, 0, 0)

    await tracker._on_event(_make_event(EventType.MESSAGE_USER_SENT, "ext-1", t0))
    await tracker._on_event(
        _make_event(EventType.MESSAGE_ASSISTANT_DONE, "ext-1", t0 + timedelta(seconds=12))
    )

    async with db() as s:
        rows = (await s.execute(select(WorkInterval))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == WorkIntervalKind.AGENT
    assert rows[0].session_id == csm_id
    assert rows[0].end_ts == t0 + timedelta(seconds=12)
    assert tracker.open_row_ids() == []


async def test_tracker_coalesce_on_double_user_sent(db):
    await _seed_session(db, "ext-2")
    tracker = WorktimeTracker(sessionmaker=db, event_stream=_fake_event_stream())
    t0 = datetime(2026, 8, 4, 10, 0, 0)

    await tracker._on_event(_make_event(EventType.MESSAGE_USER_SENT, "ext-2", t0))
    # Second USER_SENT without a DONE — should close the first at t0+5 and open new.
    await tracker._on_event(
        _make_event(EventType.MESSAGE_USER_SENT, "ext-2", t0 + timedelta(seconds=5))
    )
    await tracker._on_event(
        _make_event(EventType.MESSAGE_ASSISTANT_DONE, "ext-2", t0 + timedelta(seconds=20))
    )

    async with db() as s:
        rows = (
            await s.execute(select(WorkInterval).order_by(WorkInterval.start_ts))
        ).scalars().all()
    assert len(rows) == 2
    assert rows[0].end_ts == t0 + timedelta(seconds=5)  # closed at coalesce point
    assert rows[1].end_ts == t0 + timedelta(seconds=20)


async def test_tracker_terminal_event_closes_open(db):
    await _seed_session(db, "ext-3")
    tracker = WorktimeTracker(sessionmaker=db, event_stream=_fake_event_stream())
    t0 = datetime(2026, 8, 4, 10, 0, 0)

    await tracker._on_event(_make_event(EventType.MESSAGE_USER_SENT, "ext-3", t0))
    # No ASSISTANT_DONE; process died.
    await tracker._on_event(
        _make_event(EventType.SESSION_CRASHED, "ext-3", t0 + timedelta(seconds=8))
    )

    async with db() as s:
        rows = (await s.execute(select(WorkInterval))).scalars().all()
    assert len(rows) == 1
    assert rows[0].end_ts == t0 + timedelta(seconds=8)
    assert tracker.open_row_ids() == []


async def test_tracker_sweeper_closes_overdue_open_row(db):
    """`_sweep_once` closes any open agent row older than the 30min cap,
    prunes the internal map, and marks the row source=REAP — without
    disturbing rows that are still within the cap."""
    tracker = WorktimeTracker(sessionmaker=db, event_stream=_fake_event_stream())
    now = datetime.utcnow().replace(microsecond=0)

    # Seed one overdue row (started 40 min ago) + one fresh row (10 min ago).
    async with db() as s:
        overdue = WorkInterval(
            kind=WorkIntervalKind.AGENT,
            session_id=None,
            start_ts=now - timedelta(minutes=40),
            end_ts=None,
            source=WorkIntervalSource.EVENT,
        )
        fresh = WorkInterval(
            kind=WorkIntervalKind.AGENT,
            session_id=None,
            start_ts=now - timedelta(minutes=10),
            end_ts=None,
            source=WorkIntervalSource.EVENT,
        )
        s.add_all([overdue, fresh])
        await s.commit()
        overdue_id, fresh_id = overdue.id, fresh.id
    # Mirror what a live tracker would hold in memory.
    tracker._open = {"ext-overdue": overdue_id, "ext-fresh": fresh_id}

    await tracker._sweep_once()

    async with db() as s:
        rows = {
            r.id: r
            for r in (await s.execute(select(WorkInterval))).scalars().all()
        }
    assert rows[overdue_id].end_ts is not None
    assert rows[overdue_id].source == WorkIntervalSource.REAP
    assert (
        rows[overdue_id].end_ts - rows[overdue_id].start_ts
    ).total_seconds() == 30 * 60
    assert rows[fresh_id].end_ts is None  # untouched
    # Internal map pruned for the swept row, kept for the fresh one.
    assert "ext-overdue" not in tracker._open
    assert tracker._open.get("ext-fresh") == fresh_id


async def test_tracker_30min_safety_cap(db):
    await _seed_session(db, "ext-4")
    tracker = WorktimeTracker(sessionmaker=db, event_stream=_fake_event_stream())
    t0 = datetime(2026, 8, 4, 10, 0, 0)

    await tracker._on_event(_make_event(EventType.MESSAGE_USER_SENT, "ext-4", t0))
    # DONE arrives 2h later — must be capped at start + 30min.
    await tracker._on_event(
        _make_event(EventType.MESSAGE_ASSISTANT_DONE, "ext-4", t0 + timedelta(hours=2))
    )

    async with db() as s:
        rows = (await s.execute(select(WorkInterval))).scalars().all()
    assert len(rows) == 1
    assert (rows[0].end_ts - rows[0].start_ts).total_seconds() == 30 * 60


# ---- Heartbeat + reap ----------------------------------------------------


async def test_heartbeat_extend_then_close_via_sweeper(db):
    """A short-window heartbeat pair extends the same interval; the sweeper
    then closes it when we push last_seen past the 60s grace."""
    hb = HeartbeatManager(sessionmaker=db)
    # Do NOT call start(); avoid launching the background sweep task in tests.

    r1 = await hb.heartbeat()
    row_id_1 = r1["open_row_id"]
    r2 = await hb.heartbeat()  # < 60s gap — same row
    assert r2["open_row_id"] == row_id_1
    assert r2["reopened"] is False

    # Simulate 90s of silence, then manual sweep.
    hb._last_seen_ts = hb._last_seen_ts - timedelta(seconds=90)
    await hb._sweep_once()

    async with db() as s:
        rows = (await s.execute(select(WorkInterval))).scalars().all()
    assert len(rows) == 1
    assert rows[0].end_ts is not None
    assert rows[0].kind == WorkIntervalKind.HUMAN


async def test_reap_orphans_on_boot_closes_dangling_rows(db):
    """A row from a previous run with end_ts=NULL must be closed on boot."""
    old_start = datetime(2026, 8, 4, 8, 0, 0)
    async with db() as s:
        s.add_all(
            [
                WorkInterval(
                    kind=WorkIntervalKind.AGENT,
                    session_id="fake-sid",
                    start_ts=old_start,
                    end_ts=None,
                    source=WorkIntervalSource.EVENT,
                ),
                WorkInterval(
                    kind=WorkIntervalKind.HUMAN,
                    session_id=None,
                    start_ts=old_start,
                    end_ts=None,
                    source=WorkIntervalSource.HEARTBEAT,
                ),
            ]
        )
        await s.commit()

    svc = WorktimeService(sessionmaker=db)
    closed = await svc.reap_orphans_on_boot()
    assert closed == 2

    async with db() as s:
        rows = (await s.execute(select(WorkInterval))).scalars().all()
    for r in rows:
        assert r.end_ts is not None
        assert r.source == WorkIntervalSource.REAP
        # Agent row capped at 30min; human row would be capped at 24h.
        span = (r.end_ts - r.start_ts).total_seconds()
        if r.kind == WorkIntervalKind.AGENT:
            assert span <= 30 * 60 + 1
        else:
            assert span <= 24 * 60 * 60 + 1


async def test_live_totals_counts_today_and_open(db):
    """LiveTotals must sum today's closed intervals AND report open ones."""
    svc = WorktimeService(sessionmaker=db)
    # Insert one closed agent interval (10s) starting a couple hours ago,
    # and one still-open agent interval (start_ts = 15s ago).
    now = datetime.utcnow().replace(microsecond=0)
    async with db() as s:
        s.add_all(
            [
                WorkInterval(
                    kind=WorkIntervalKind.AGENT,
                    session_id="a",
                    start_ts=now - timedelta(minutes=90),
                    end_ts=now - timedelta(minutes=90) + timedelta(seconds=10),
                    source=WorkIntervalSource.EVENT,
                ),
                WorkInterval(
                    kind=WorkIntervalKind.AGENT,
                    session_id="b",
                    start_ts=now - timedelta(seconds=15),
                    end_ts=None,
                    source=WorkIntervalSource.EVENT,
                ),
                WorkInterval(
                    kind=WorkIntervalKind.HUMAN,
                    session_id=None,
                    start_ts=now - timedelta(minutes=5),
                    end_ts=now - timedelta(minutes=4),
                    source=WorkIntervalSource.HEARTBEAT,
                ),
            ]
        )
        await s.commit()

    totals = await svc.live_totals()
    # Closed agent contributed 10s. Open agent contributed ~15s (± clock jitter).
    assert totals.today_agent_sec >= 10 + 14
    assert totals.open_agent_count == 1
    assert totals.open_agent_sec >= 14
    assert totals.today_human_sec >= 59


async def test_live_totals_all_time_includes_pre_today_rows(db):
    """`all_*_sec` must include closed rows from days before today, while
    `today_*_sec` must not."""
    svc = WorktimeService(sessionmaker=db)
    now = datetime.utcnow().replace(microsecond=0)
    # Old row: fully in the past (5 days ago, 20s long).
    old_start = now - timedelta(days=5)
    old_end = old_start + timedelta(seconds=20)
    # Recent row: today, 30s long.
    recent_start = now - timedelta(minutes=2)
    recent_end = recent_start + timedelta(seconds=30)
    async with db() as s:
        s.add_all(
            [
                WorkInterval(
                    kind=WorkIntervalKind.AGENT,
                    session_id=None,
                    start_ts=old_start,
                    end_ts=old_end,
                    source=WorkIntervalSource.EVENT,
                ),
                WorkInterval(
                    kind=WorkIntervalKind.HUMAN,
                    session_id=None,
                    start_ts=old_start,
                    end_ts=old_end,
                    source=WorkIntervalSource.HEARTBEAT,
                ),
                WorkInterval(
                    kind=WorkIntervalKind.AGENT,
                    session_id=None,
                    start_ts=recent_start,
                    end_ts=recent_end,
                    source=WorkIntervalSource.EVENT,
                ),
                WorkInterval(
                    kind=WorkIntervalKind.HUMAN,
                    session_id=None,
                    start_ts=recent_start,
                    end_ts=recent_end,
                    source=WorkIntervalSource.HEARTBEAT,
                ),
            ]
        )
        await s.commit()

    totals = await svc.live_totals()
    # all_* = old (20s) + recent (30s) = 50s per kind
    assert totals.all_agent_sec == 50
    assert totals.all_human_sec == 50
    # today_* excludes the 5-day-old rows
    assert totals.today_agent_sec == 30
    assert totals.today_human_sec == 30

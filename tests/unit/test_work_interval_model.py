"""Smoke round-trip for the WorkInterval ORM row."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest
from csm.models import Base, WorkInterval, WorkIntervalKind, WorkIntervalSource
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


async def test_work_interval_round_trip(db):
    start = datetime(2026, 8, 4, 10, 0, 0)
    async with db() as s:
        row = WorkInterval(
            kind=WorkIntervalKind.AGENT,
            session_id="sid-abc",
            start_ts=start,
            end_ts=start + timedelta(seconds=47),
            source=WorkIntervalSource.EVENT,
        )
        s.add(row)
        await s.commit()
        got = await s.get(WorkInterval, row.id)
        assert got is not None
        assert got.kind == WorkIntervalKind.AGENT
        assert got.session_id == "sid-abc"
        assert got.end_ts == start + timedelta(seconds=47)
        assert got.source == WorkIntervalSource.EVENT
        assert got.created_at is not None


async def test_work_interval_open_interval_nullable_end(db):
    async with db() as s:
        row = WorkInterval(
            kind=WorkIntervalKind.HUMAN,
            session_id=None,
            start_ts=datetime(2026, 8, 4, 10, 0, 0),
            source=WorkIntervalSource.HEARTBEAT,
        )
        s.add(row)
        await s.commit()

        # Query open intervals via end_ts IS NULL — reap path uses this.
        result = await s.execute(select(WorkInterval).where(WorkInterval.end_ts.is_(None)))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].kind == WorkIntervalKind.HUMAN
        assert rows[0].session_id is None

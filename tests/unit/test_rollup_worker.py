"""Unit tests for RollupWorker — verify aggregation + TTL on an in-memory DB."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from csm.models import HourlyRollup, RawTokenEvent, UserPreference
from csm.models.base import Base
from csm.modules.token.rollup import RollupWorker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def sm():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(engine, expire_on_commit=False)
    yield SM
    await engine.dispose()


async def _insert(
    sm,
    ts: datetime,
    model: str | None = "claude-opus-4-7",
    proj="/p",
    in_t=10,
    out=5,
    cc=0,
    cr=0,
    agent="claude",
):
    async with sm() as db:
        db.add(RawTokenEvent(
            ts=ts, model=model, project_path=proj,
            input_tokens=in_t, cache_creation_tokens=cc, cache_read_tokens=cr, output_tokens=out,
            estimated_cost_usd=0.001, agent=agent,
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_rollup_aggregates_past_hours(sm):
    # Three events across two past hours
    base = datetime.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    await _insert(sm, base + timedelta(minutes=5), in_t=10, out=5)
    await _insert(sm, base + timedelta(minutes=50), in_t=20, out=7)
    await _insert(sm, base + timedelta(hours=1, minutes=10), in_t=100, out=50)

    worker = RollupWorker(sessionmaker=sm)
    result = await worker.run_once()
    assert result["rolled_buckets"] == 2  # two hour buckets

    async with sm() as db:
        rows = (await db.execute(select(HourlyRollup).order_by(HourlyRollup.bucket_hour))).scalars().all()
    assert len(rows) == 2
    assert rows[0].input_tokens == 30 and rows[0].output_tokens == 12 and rows[0].msg_count == 2
    assert rows[1].input_tokens == 100 and rows[1].output_tokens == 50 and rows[1].msg_count == 1


@pytest.mark.asyncio
async def test_rollup_skips_current_hour(sm):
    """Current-hour rows are still being written; do NOT roll them."""
    now = datetime.utcnow()
    # Anchor the row to the *current* hour bucket regardless of clock minute.
    in_current_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=30)
    if in_current_hour > now:
        in_current_hour = now.replace(second=0, microsecond=0)
    await _insert(sm, in_current_hour, in_t=999)
    worker = RollupWorker(sessionmaker=sm)
    result = await worker.run_once()
    assert result["rolled_buckets"] == 0


@pytest.mark.asyncio
async def test_rollup_idempotent(sm):
    base = datetime.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    # NULL model used to bypass SQLite's unique index and insert a duplicate
    # on every rollup tick.
    await _insert(sm, base + timedelta(minutes=5), model=None, in_t=10, out=5)

    worker = RollupWorker(sessionmaker=sm)
    await worker.run_once()
    await worker.run_once()  # second run — should upsert not duplicate

    async with sm() as db:
        rows = (await db.execute(select(HourlyRollup))).scalars().all()
    assert len(rows) == 1
    assert rows[0].input_tokens == 10


@pytest.mark.asyncio
async def test_rollup_separates_agents_in_same_hour_model_and_project(sm):
    base = datetime.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    await _insert(sm, base + timedelta(minutes=5), model="shared", agent="claude", in_t=10)
    await _insert(sm, base + timedelta(minutes=10), model="shared", agent="codex", in_t=20)

    result = await RollupWorker(sessionmaker=sm).run_once()

    assert result["rolled_buckets"] == 2
    async with sm() as db:
        rows = (await db.execute(select(HourlyRollup))).scalars().all()
    assert {(row.agent, row.input_tokens) for row in rows} == {
        ("claude", 10),
        ("codex", 20),
    }


@pytest.mark.asyncio
async def test_ttl_deletes_old_raw(sm):
    base = datetime.utcnow()
    await _insert(sm, base - timedelta(days=8), in_t=1)   # outside retention
    await _insert(sm, base - timedelta(days=8), in_t=2)
    await _insert(sm, base - timedelta(days=3), in_t=3)   # inside retention
    await _insert(sm, base - timedelta(hours=1), in_t=4)  # current

    worker = RollupWorker(sessionmaker=sm, retention_days=7)
    result = await worker.run_once()
    # rollup runs first; only past-hour rows go into rollup, all 4 hit it
    assert result["deleted_raw"] == 2

    async with sm() as db:
        remaining = (await db.execute(select(RawTokenEvent))).scalars().all()
    assert len(remaining) == 2
    # the 3-day-old and the 1-hour-old should remain
    inputs = sorted(r.input_tokens for r in remaining)
    assert inputs == [3, 4]


async def _set_retention_pref(sm, days: int) -> None:
    async with sm() as db:
        db.add(UserPreference(id=1, default_agent="claude", raw_event_retention_days=days))
        await db.commit()


@pytest.mark.asyncio
async def test_ttl_uses_pref_over_constructor_default(sm):
    """The live user_preference window wins over the constructor fallback, so
    changing it via the API retunes TTL without reconstructing the worker."""
    base = datetime.utcnow()
    await _insert(sm, base - timedelta(days=8), in_t=1)   # old
    await _insert(sm, base - timedelta(days=3), in_t=2)   # recent
    await _set_retention_pref(sm, 7)

    # Constructor says "keep forever" (0) but the pref says 7 → pref wins.
    worker = RollupWorker(sessionmaker=sm, retention_days=0)
    result = await worker.run_once()
    assert result["deleted_raw"] == 1  # the 8-day-old row

    async with sm() as db:
        remaining = (await db.execute(select(RawTokenEvent))).scalars().all()
    assert sorted(r.input_tokens for r in remaining) == [2]


@pytest.mark.asyncio
async def test_ttl_pref_zero_keeps_everything(sm):
    """A pref of 0 disables TTL even when the constructor fallback is positive."""
    base = datetime.utcnow()
    await _insert(sm, base - timedelta(days=90), in_t=1)
    await _set_retention_pref(sm, 0)

    worker = RollupWorker(sessionmaker=sm, retention_days=7)
    result = await worker.run_once()
    assert result["deleted_raw"] == 0

    async with sm() as db:
        remaining = (await db.execute(select(RawTokenEvent))).scalars().all()
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_rollup_per_model_project_grouping(sm):
    base = datetime.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    await _insert(sm, base + timedelta(minutes=5), model="opus", proj="/p1", in_t=10)
    await _insert(sm, base + timedelta(minutes=10), model="opus", proj="/p1", in_t=20)
    await _insert(sm, base + timedelta(minutes=15), model="opus", proj="/p2", in_t=5)
    await _insert(sm, base + timedelta(minutes=20), model="sonnet", proj="/p1", in_t=100)

    worker = RollupWorker(sessionmaker=sm)
    result = await worker.run_once()
    # 3 buckets: opus/p1, opus/p2, sonnet/p1
    assert result["rolled_buckets"] == 3
    async with sm() as db:
        rows = (await db.execute(select(HourlyRollup))).scalars().all()
    by_key = {(r.model, r.project_path): r.input_tokens for r in rows}
    assert by_key[("opus", "/p1")] == 30
    assert by_key[("opus", "/p2")] == 5
    assert by_key[("sonnet", "/p1")] == 100

"""TrendQueries.history() granularity coverage — esp. new minute / 5min buckets."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from csm.models import HourlyRollup, RawTokenEvent
from csm.models.base import Base
from csm.modules.token.trend import TrendQueries
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


async def _ev(
    sm,
    ts: datetime,
    tokens: int = 1000,
    *,
    agent: str = "claude",
    source: str | None = None,
):
    async with sm() as db:
        db.add(RawTokenEvent(
            ts=ts, model="sonnet",
            input_tokens=tokens, cache_creation_tokens=0,
            cache_read_tokens=0, output_tokens=0, estimated_cost_usd=0.01,
            agent=agent, source=source,
        ))
        await db.commit()


async def _rollup(
    sm,
    ts: datetime,
    tokens: int,
    *,
    agent: str,
    model: str = "sonnet",
    project: str = "/p",
):
    async with sm() as db:
        db.add(HourlyRollup(
            bucket_hour=ts,
            model=model,
            project_path=project,
            agent=agent,
            input_tokens=tokens,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.01,
            msg_count=1,
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_minute_granularity_one_bucket_per_minute(sm):
    # Anchor at a stable instant well inside [start, end] window.
    anchor = datetime(2026, 6, 23, 5, 30, 0)
    await _ev(sm, anchor)                            # 05:30
    await _ev(sm, anchor + timedelta(minutes=1))     # 05:31
    await _ev(sm, anchor + timedelta(minutes=2))     # 05:32
    rows = await TrendQueries(sm).history(
        start=anchor - timedelta(minutes=5),
        end=anchor + timedelta(minutes=10),
        granularity="minute",
    )
    buckets = [r["bucket"] for r in rows]
    assert len(buckets) == 3
    assert buckets == sorted(buckets)
    for b in buckets:
        assert b.endswith(":00") and len(b) == 19


@pytest.mark.asyncio
async def test_5min_granularity_groups_same_5min_window(sm):
    # Anchor at a 5-min boundary; two events inside [05:30, 05:35), one in [05:25, 05:30).
    base = datetime(2026, 6, 23, 5, 30, 0)
    await _ev(sm, base + timedelta(seconds=30))      # 05:30:30 → bucket 05:30
    await _ev(sm, base + timedelta(minutes=2))       # 05:32:00 → bucket 05:30
    await _ev(sm, base - timedelta(minutes=7))       # 05:23:00 → bucket 05:20
    rows = await TrendQueries(sm).history(
        start=base - timedelta(minutes=15),
        end=base + timedelta(minutes=15),
        granularity="5min",
    )
    buckets = [r["bucket"] for r in rows]
    assert len(set(buckets)) == 2
    newer = max(rows, key=lambda r: r["bucket"])
    assert newer["msg_count"] == 2


@pytest.mark.asyncio
async def test_hour_granularity_still_works(sm):
    # Regression guard: existing 'hour' default branch unchanged.
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    await _ev(sm, now - timedelta(minutes=15))
    await _ev(sm, now - timedelta(minutes=45))
    rows = await TrendQueries(sm).history(hours=2, granularity="hour")
    assert len(rows) == 1
    assert rows[0]["msg_count"] == 2


@pytest.mark.asyncio
async def test_30d_history_keeps_rollup_granularity_and_agent_scope(sm):
    start = datetime(2026, 7, 1)
    end = datetime(2026, 8, 1)
    await _rollup(sm, datetime(2026, 7, 10, 8), 9000, agent="claude")
    await _rollup(sm, datetime(2026, 7, 26, 8), 200, agent="codex")
    # Same daily bucket exists in raw + rollup. Raw is authoritative and the
    # bucket must appear once, not once per source/granularity.
    await _rollup(sm, datetime(2026, 7, 30, 8), 9999, agent="codex")
    await _ev(sm, datetime(2026, 7, 30, 9), 1000, agent="codex")

    rows = await TrendQueries(sm).history(
        start=start,
        end=end,
        granularity="day",
        filters={"agent": ["codex"]},
    )

    assert [row["bucket"] for row in rows] == [
        "2026-07-26 00:00:00",
        "2026-07-30 00:00:00",
    ]
    assert [row["input_tokens"] for row in rows] == [200, 1000]


@pytest.mark.asyncio
async def test_history_skips_rollup_when_filter_dimension_is_not_retained(sm):
    start = datetime(2026, 7, 1)
    end = datetime(2026, 8, 1)
    await _rollup(sm, datetime(2026, 7, 10, 8), 9000, agent="codex")
    await _ev(
        sm,
        datetime(2026, 7, 30, 9),
        1000,
        agent="codex",
        source="interactive",
    )

    rows = await TrendQueries(sm).history(
        start=start,
        end=end,
        granularity="day",
        filters={"agent": ["codex"], "source": ["interactive"]},
    )

    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 1000


@pytest.mark.asyncio
async def test_data_range_follows_agent_scope_without_overlap(sm):
    await _rollup(sm, datetime(2026, 7, 10, 8), 9000, agent="claude")
    await _rollup(sm, datetime(2026, 7, 25, 8), 200, agent="codex")
    await _ev(sm, datetime(2026, 7, 25, 9), 1000, agent="codex")

    result = await TrendQueries(sm).data_range(filters={"agent": ["codex"]})

    assert result["earliest"].startswith("2026-07-25")
    assert result["event_count"] == 2


@pytest.mark.asyncio
async def test_data_range_does_not_count_earliest_raw_hour_twice(sm):
    await _rollup(sm, datetime(2026, 7, 25, 9), 1000, agent="codex")
    await _ev(sm, datetime(2026, 7, 25, 9, 30), 1000, agent="codex")

    result = await TrendQueries(sm).data_range(filters={"agent": ["codex"]})

    assert result["earliest"].startswith("2026-07-25T09:30")
    assert result["event_count"] == 1

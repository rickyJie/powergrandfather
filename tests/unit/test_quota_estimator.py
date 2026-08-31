"""Unit tests for QuotaEstimator (no-ceiling, per ADR-0001)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from csm.models import HitObservation, RawTokenEvent
from csm.models.base import Base
from csm.modules.token.quota import QuotaEstimator
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


async def _hit(sm, total: int, ts: datetime | None = None):
    async with sm() as db:
        db.add(HitObservation(
            ts=ts or datetime.utcnow(),
            msg_count_5h=1, cc_tokens_5h=0, cr_tokens_5h=0,
            input_tokens_5h=total // 2, output_tokens_5h=total - total // 2,
        ))
        await db.commit()


async def _raw(sm, total: int, ts: datetime | None = None):
    async with sm() as db:
        db.add(RawTokenEvent(
            ts=ts or datetime.utcnow(), model="claude",
            input_tokens=total, cache_creation_tokens=0,
            cache_read_tokens=0, output_tokens=0, estimated_cost_usd=0,
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_absolute_tokens_reported(sm):
    est = QuotaEstimator(sm)
    await _raw(sm, 300)
    r = await est.estimate()
    assert r["current_tokens"] == 300
    # ADR-0001: no percentage / denominator surfaced
    assert "current_pct" not in r
    assert "denominator_tokens" not in r


@pytest.mark.asyncio
async def test_runway_equals_window_reset(sm):
    """With no ceiling (ADR-0001), runway degrades to window_reset_in_minutes."""
    est = QuotaEstimator(sm)
    now = datetime.utcnow()
    # earliest event 4h40min ago → ages out in ~20 min
    await _raw(sm, 100, ts=now - timedelta(hours=4, minutes=40))
    await _raw(sm, 50, ts=now - timedelta(minutes=1))
    r = await est.estimate()
    assert r["window_reset_in_minutes"] is not None
    assert 18 <= r["window_reset_in_minutes"] <= 22
    assert r["runway_minutes"] == r["window_reset_in_minutes"]


@pytest.mark.asyncio
async def test_confidence_tiers(sm):
    est = QuotaEstimator(sm, min_observations=3)
    # 0 obs → insufficient-data
    r = await est.estimate()
    assert r["confidence"] == "insufficient-data"
    # 3 obs → low
    for _ in range(3): await _hit(sm, 500)
    r = await est.estimate()
    assert r["confidence"] == "low"
    # 5 obs → medium
    for _ in range(2): await _hit(sm, 500)
    r = await est.estimate()
    assert r["confidence"] == "medium"
    # 10 obs → high
    for _ in range(5): await _hit(sm, 500)
    r = await est.estimate()
    assert r["confidence"] == "high"


@pytest.mark.asyncio
async def test_zero_burn_no_runway(sm):
    est = QuotaEstimator(sm)
    now = datetime.utcnow()
    # event older than 10 min → counted in 5h window but NOT in burn
    await _raw(sm, 100, ts=now - timedelta(minutes=30))
    r = await est.estimate()
    assert r["current_tokens"] == 100
    assert r["burn_per_min"] == 0.0
    # runway falls back to window_reset_in_minutes
    assert r["runway_minutes"] is not None
    assert r["runway_minutes"] == r["window_reset_in_minutes"]

"""TrendQueries.current_window() model / project filter coverage.

Regression guard for the GLM-vs-Claude cache-hit dilution bug: mixing
non-caching model rows (GLM emits 0 cache_read) into current_window()
tanks the hero cache_hit% number even when real Claude sessions cache
fine. Filter must be able to exclude those rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from csm.models import RawTokenEvent
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


async def _seed(sm, *, model: str, cr: int, project: str = "/p"):
    async with sm() as db:
        db.add(RawTokenEvent(
            ts=datetime.utcnow() - timedelta(minutes=5),
            model=model, project_path=project,
            input_tokens=100, cache_creation_tokens=0,
            cache_read_tokens=cr, output_tokens=0, estimated_cost_usd=0.01,
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_no_filter_mixes_all_models(sm):
    await _seed(sm, model="claude-opus-4-7", cr=900)
    await _seed(sm, model="glm-5-2", cr=0)
    res = await TrendQueries(sm).current_window(hours=1)
    # 900 cache_read + 200 input across two events → hit ratio dragged down by glm
    assert res["cache_read_tokens"] == 900
    assert res["input_tokens"] == 200
    assert res["msg_count"] == 2


@pytest.mark.asyncio
async def test_model_include_filter_isolates_claude(sm):
    await _seed(sm, model="claude-opus-4-7", cr=900)
    await _seed(sm, model="glm-5-2", cr=0)
    res = await TrendQueries(sm).current_window(
        hours=1, filters={"model": ["claude-opus-4-7"]},
    )
    assert res["msg_count"] == 1
    assert res["cache_read_tokens"] == 900
    assert res["input_tokens"] == 100


@pytest.mark.asyncio
async def test_empty_filter_value_is_noop(sm):
    await _seed(sm, model="claude-opus-4-7", cr=900)
    await _seed(sm, model="glm-5-2", cr=0)
    # None / [] / "" should all be treated as "no filter".
    for v in (None, [], ""):
        res = await TrendQueries(sm).current_window(
            hours=1, filters={"model": v},
        )
        assert res["msg_count"] == 2, f"filter value {v!r} should be no-op"


@pytest.mark.asyncio
async def test_project_filter_also_wires(sm):
    # Confirm _apply_filters routes non-model keys too — the /current route
    # exposes project/source/etc, so a regression here would silently break UI.
    await _seed(sm, model="claude-opus-4-7", cr=900, project="/a")
    await _seed(sm, model="claude-opus-4-7", cr=500, project="/b")
    res = await TrendQueries(sm).current_window(
        hours=1, filters={"project": ["/a"]},
    )
    assert res["msg_count"] == 1
    assert res["cache_read_tokens"] == 900

"""Token aggregator unit tests — feed mock events, verify persistence + aggregation."""
from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import Base, RawTokenEvent
from csm.modules.token.aggregator import TokenAggregator, estimate_cost, model_family
from csm.modules.token.trend import TrendQueries
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def setup():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    es = EventStream(projects_root=Path(tempfile.gettempdir()), poll_interval_sec=10.0, watchdog_interval_sec=10.0)
    agg = TokenAggregator(sessionmaker=sm, event_stream=es)
    await agg.start()
    yield agg, sm, es
    await agg.stop()
    await es.stop()
    await engine.dispose()
    os.unlink(db_path)


def test_model_family():
    assert model_family("claude-opus-4-7") == "opus"
    assert model_family("claude-sonnet-4-6") == "sonnet"
    assert model_family("claude-haiku-4-5") == "haiku"
    assert model_family(None) == "sonnet"
    assert model_family("") == "sonnet"


def test_cost_estimation_known_ratios():
    # 1M input opus ≈ $15
    assert estimate_cost(1_000_000, 0, 0, 0, "opus") == pytest.approx(15.0)
    # 1M cache_read sonnet ≈ $0.30
    assert estimate_cost(0, 0, 1_000_000, 0, "sonnet") == pytest.approx(0.30)


async def test_usage_event_persisted(setup):
    agg, sm, es = setup
    e = Event(
        type=EventType.USAGE_RECORDED,
        ts=datetime.now(UTC),
        session_id="sid-abc",
        project_path="/tmp/p",
        payload={
            "model": "claude-sonnet-4-6",
            "input_tokens": 100,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 1000,
            "output_tokens": 50,
            "is_subagent": False,
        },
    )
    await es.emit(e)
    # Subscribers are called synchronously by emit.
    async with sm() as db:
        from sqlalchemy import select as _select
        rows = (await db.execute(_select(RawTokenEvent))).scalars().all()
        assert len(rows) == 1
        r = rows[0]
        assert r.external_session_id == "sid-abc"
        assert r.input_tokens == 100
        assert r.cache_creation_tokens == 200
        assert r.cache_read_tokens == 1000
        assert r.output_tokens == 50
        # Expected cost: sonnet 100*3 + 200*3.75 + 1000*0.3 + 50*15 = 300+750+300+750 = 2100 / 1M = 0.0021
        assert r.estimated_cost_usd == pytest.approx(0.0021, rel=1e-3)


async def test_rate_limit_hit_snapshots_window(setup):
    agg, sm, es = setup
    now = datetime.now(UTC)
    # Seed two usage events: one inside window, one outside.
    for offset_min, in_t in [(10, 100), (400, 5000)]:
        e = Event(
            type=EventType.USAGE_RECORDED,
            ts=now - timedelta(minutes=offset_min),
            session_id="sid",
            project_path="/tmp",
            payload={
                "model": "claude-sonnet-4-6",
                "input_tokens": in_t,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
                "is_subagent": False,
            },
        )
        await es.emit(e)

    # Now the hit event.
    hit = Event(
        type=EventType.RATE_LIMIT_HIT,
        ts=now,
        session_id="sid",
        project_path="/tmp",
        payload={"reset_text": "8:30pm Asia/Shanghai"},
    )
    await es.emit(hit)

    from csm.models import HitObservation
    from sqlalchemy import select as _select
    async with sm() as db:
        rows = (await db.execute(_select(HitObservation))).scalars().all()
        assert len(rows) == 1
        # Only the in-window event (100 input tokens) should be counted.
        # The 400-minute-old one (> 5h) excluded.
        assert rows[0].input_tokens_5h == 100
        assert rows[0].msg_count_5h == 1
        assert rows[0].reset_text == "8:30pm Asia/Shanghai"


async def test_trend_current_window(setup):
    agg, sm, es = setup
    now = datetime.now(UTC)
    for i in range(3):
        await es.emit(Event(
            type=EventType.USAGE_RECORDED,
            ts=now - timedelta(minutes=i * 10),
            session_id="s",
            project_path="/tmp",
            payload={
                "model": "claude-opus-4-7",
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30,
                "output_tokens": 5,
                "is_subagent": False,
            },
        ))
    trend = TrendQueries(sm)
    res = await trend.current_window(hours=5.0)
    assert res["msg_count"] == 3
    assert res["input_tokens"] == 30
    assert res["cache_creation_tokens"] == 60
    assert res["cache_read_tokens"] == 90
    assert res["output_tokens"] == 15


async def test_trend_top_consumers(setup):
    agg, sm, es = setup
    now = datetime.now(UTC)
    for sid, cc in [("s1", 1000), ("s2", 500), ("s3", 2000)]:
        await es.emit(Event(
            type=EventType.USAGE_RECORDED,
            ts=now,
            session_id=sid,
            project_path="/tmp",
            payload={
                "model": "claude-sonnet-4-6",
                "input_tokens": 0,
                "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
                "is_subagent": False,
            },
        ))
    trend = TrendQueries(sm)
    top = await trend.top_consumers(scope="session", hours=1, limit=3)
    # Highest cc first.
    assert top[0]["entity"] == "s3"
    assert top[0]["cache_creation_tokens"] == 2000
    assert top[1]["entity"] == "s1"
    assert top[2]["entity"] == "s2"


# ---- edge cases (added in deepen round) ----
async def test_rate_limit_hit_with_empty_window(setup):
    """No prior usage → HitObservation row records zeros, not error."""
    agg, sm, es = setup
    await es.emit(Event(
        type=EventType.RATE_LIMIT_HIT,
        ts=datetime.now(UTC),
        session_id="sid-empty",
        project_path="/tmp",
        payload={"reset_text": "3:00pm Asia/Shanghai"},
    ))
    from csm.models import HitObservation
    from sqlalchemy import select as _select
    async with sm() as db:
        rows = (await db.execute(_select(HitObservation))).scalars().all()
        assert len(rows) == 1
        assert rows[0].msg_count_5h == 0
        assert rows[0].cc_tokens_5h == 0


async def test_usage_event_with_unknown_model_defaults_sonnet(setup):
    """Unknown model name should be cost-estimated as Sonnet (per model_family fallback)."""
    agg, sm, es = setup
    await es.emit(Event(
        type=EventType.USAGE_RECORDED,
        ts=datetime.now(UTC),
        session_id="sid-x",
        project_path="/tmp",
        payload={
            "model": "claude-future-model-9000",
            "input_tokens": 1000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
            "is_subagent": False,
        },
    ))
    from sqlalchemy import select as _select
    async with sm() as db:
        rows = (await db.execute(_select(RawTokenEvent))).scalars().all()
        assert len(rows) == 1
        # Sonnet input rate = $3/M → 1000 tokens = $0.003
        assert rows[0].estimated_cost_usd == pytest.approx(0.003, rel=1e-3)


async def test_history_returns_empty_for_no_data(setup):
    agg, sm, es = setup
    trend = TrendQueries(sm)
    rows = await trend.history(hours=24, granularity="hour")
    assert rows == []


async def test_top_consumers_respects_limit(setup):
    agg, sm, es = setup
    now = datetime.now(UTC)
    for i in range(5):
        await es.emit(Event(
            type=EventType.USAGE_RECORDED,
            ts=now,
            session_id=f"s{i}",
            project_path="/tmp",
            payload={
                "model": "claude-sonnet-4-6",
                "input_tokens": 0,
                "cache_creation_input_tokens": 100 * (i + 1),
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
                "is_subagent": False,
            },
        ))
    trend = TrendQueries(sm)
    top = await trend.top_consumers(scope="session", hours=1, limit=2)
    assert len(top) == 2
    assert top[0]["entity"] == "s4"  # 500 cc
    assert top[1]["entity"] == "s3"  # 400 cc


async def test_subagent_flag_persisted(setup):
    agg, sm, es = setup
    await es.emit(Event(
        type=EventType.USAGE_RECORDED,
        ts=datetime.now(UTC),
        session_id="sid-sub",
        project_path="/tmp",
        payload={
            "model": "claude-sonnet-4-6",
            "input_tokens": 10, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 0,
            "is_subagent": True,
        },
    ))
    from sqlalchemy import select as _select
    async with sm() as db:
        row = (await db.execute(_select(RawTokenEvent))).scalar_one()
        assert row.is_subagent is True


async def test_current_window_zero_when_no_events(setup):
    agg, sm, es = setup
    trend = TrendQueries(sm)
    res = await trend.current_window(hours=5.0)
    assert res["msg_count"] == 0
    assert res["total_tokens"] == 0
    assert res["estimated_cost_usd"] == 0.0

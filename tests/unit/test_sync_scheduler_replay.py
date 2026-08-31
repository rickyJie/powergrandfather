"""Tests for SyncTickScheduler + startup replay (Phase 3c + 3d).

Covers:
- Scheduler start/stop lifecycle (no-hang on stop)
- cleanup_stale_pending_ledger sweeps rows > cutoff to failed_terminal
- replay_pending_fanout_ledger picks up ONLY phase2_done (not pending)
- attempt_count >= 3 → failed_terminal
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from csm.models import Base
from csm.models.fanout_ledger import FanoutLedger
from csm.models.instruction import Instruction
from csm.modules.sync.orchestrator import SyncOrchestrator
from csm.modules.sync.scheduler import SyncTickScheduler
from csm.utils.time import now_utc_naive
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def sm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield sm
    finally:
        await engine.dispose()


def _mk_orch(sm):
    reg = MagicMock()
    reg.names = MagicMock(return_value=["claude"])
    svc = MagicMock()
    svc.sync_by_type_id = AsyncMock(return_value=[])
    agent = MagicMock()
    agent.decide = AsyncMock(return_value=(None, {}))
    return SyncOrchestrator(sm, reg, svc, agent)


# ---------------------------------------------------------------------------
# cleanup_stale_pending_ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_stale_pending_sweeps_old_rows(sm):
    orch = _mk_orch(sm)
    now = now_utc_naive()
    async with sm() as db:
        db.add_all([
            # 45-day-old pending → should become failed_terminal
            FanoutLedger(
                ts=now - timedelta(days=45), resource_type="instruction",
                resource_id=1, body_hash="h1", target_agents=["claude"],
                status="pending", attempt_count=0,
            ),
            # 5-day-old pending → stays pending
            FanoutLedger(
                ts=now - timedelta(days=5), resource_type="instruction",
                resource_id=2, body_hash="h2", target_agents=["claude"],
                status="pending", attempt_count=0,
            ),
            # 45-day-old already done → stays done
            FanoutLedger(
                ts=now - timedelta(days=45), resource_type="instruction",
                resource_id=3, body_hash="h3", target_agents=["claude"],
                status="done", attempt_count=1,
            ),
        ])
        await db.commit()

    swept = await orch.cleanup_stale_pending_ledger(older_than_days=30)
    assert swept == 1
    async with sm() as db:
        rows = (await db.execute(select(FanoutLedger).order_by(FanoutLedger.resource_id))).scalars().all()
    statuses = [(r.resource_id, r.status) for r in rows]
    assert (1, "failed_terminal") in statuses
    assert (2, "pending") in statuses
    assert (3, "done") in statuses


# ---------------------------------------------------------------------------
# replay_pending_fanout_ledger (v7 §2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_only_processes_phase2_done_entries(sm):
    orch = _mk_orch(sm)
    async with sm() as db:
        row = Instruction(
            name="e", title="T", body="body",
            share_scope=["claude"], priority=0,
            created_at=now_utc_naive(), updated_at=now_utc_naive(),
        )
        db.add(row)
        await db.commit()
        rid = row.id
        db.add_all([
            # phase2_done → replay picks it up
            FanoutLedger(
                ts=now_utc_naive(), resource_type="instruction",
                resource_id=rid, body_hash="h", target_agents=["claude"],
                status="phase2_done", attempt_count=0,
                fanout_result_json=[
                    {"agent": "codex", "status": "ok", "detail": None},
                ],
            ),
            # pending → left alone (v7 change)
            FanoutLedger(
                ts=now_utc_naive(), resource_type="instruction",
                resource_id=rid, body_hash="h", target_agents=["claude"],
                status="pending", attempt_count=0,
            ),
        ])
        await db.commit()

    await orch.replay_pending_fanout_ledger()
    async with sm() as db:
        rows = (await db.execute(
            select(FanoutLedger).order_by(FanoutLedger.id)
        )).scalars().all()
        instr = (await db.execute(
            select(Instruction).where(Instruction.id == rid)
        )).scalar_one()
    statuses = [r.status for r in rows]
    assert statuses[0] == "done"      # was phase2_done → done
    assert statuses[1] == "pending"   # untouched
    # phase2_done replay must have stamped codex hash.
    assert "codex" in (instr.last_synced_hashes or {})


@pytest.mark.asyncio
async def test_replay_attempt_cap_reaches_failed_terminal(sm):
    """After 3 replay failures, entry status becomes failed_terminal.

    Directly exercises `_bump_attempt_or_terminal` since simulating a
    failing replay would require patching `_replay_one_phase2_done_entry`.
    """
    orch = _mk_orch(sm)
    async with sm() as db:
        entry = FanoutLedger(
            ts=now_utc_naive(), resource_type="instruction",
            resource_id=1, body_hash="h", target_agents=["c"],
            status="phase2_done", attempt_count=2,
        )
        db.add(entry)
        await db.commit()
        eid = entry.id

    await orch._bump_attempt_or_terminal(eid)
    async with sm() as db:
        e = await db.get(FanoutLedger, eid)
    assert e.attempt_count == 3
    assert e.status == "failed_terminal"


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_start_stop_drains_cleanly(sm):
    orch = _mk_orch(sm)
    scheduler = SyncTickScheduler(orch, sm, loop_sleep_sec=0.05)
    await scheduler.start()
    # Let the loop iterate a couple of times.
    await asyncio.sleep(0.15)
    await scheduler.stop()
    assert scheduler._task is None


@pytest.mark.asyncio
async def test_scheduler_stop_when_never_started_is_safe(sm):
    orch = _mk_orch(sm)
    scheduler = SyncTickScheduler(orch, sm)
    await scheduler.stop()  # no-op


@pytest.mark.asyncio
async def test_scheduler_should_tick_now_false_when_all_configs_lock(sm):
    """Default sync_mode='lock' → no scheduled ticks fire."""
    orch = _mk_orch(sm)
    scheduler = SyncTickScheduler(orch, sm)
    from csm.models.sync_config import SyncConfig
    async with sm() as db:
        db.add(SyncConfig(
            module="memory", enrolled_agents=["claude"],
            poll_interval_sec=30, enabled=True,
            updated_at=now_utc_naive(),
        ))
        await db.commit()
    assert await scheduler._should_tick_now() is False


@pytest.mark.asyncio
async def test_scheduler_should_tick_now_true_when_agent_mode_and_interval(sm):
    """sync_mode='agent' + interval>0 + never ran → True."""
    orch = _mk_orch(sm)
    scheduler = SyncTickScheduler(orch, sm)
    from csm.models.sync_config import SyncConfig
    async with sm() as db:
        cfg = SyncConfig(
            module="memory", enrolled_agents=["claude"],
            poll_interval_sec=30, enabled=True,
            updated_at=now_utc_naive(),
        )
        db.add(cfg)
        await db.commit()
        # Mutate the sync_mode + tick_interval_hours via raw SQL to
        # avoid the ORM default overrides.
        from sqlalchemy import text
        await db.execute(text(
            "UPDATE sync_config SET sync_mode='agent', "
            "tick_interval_hours=24 WHERE id = :i"
        ).bindparams(i=cfg.id))
        await db.commit()
    assert await scheduler._should_tick_now() is True


@pytest.mark.asyncio
async def test_scheduler_calls_cleanup_stale_pending_each_iteration(sm):
    """Every loop iteration wraps cleanup_stale_pending_ledger."""
    orch = _mk_orch(sm)
    orch.cleanup_stale_pending_ledger = AsyncMock(return_value=0)
    scheduler = SyncTickScheduler(orch, sm, loop_sleep_sec=0.05)
    await scheduler.start()
    await asyncio.sleep(0.15)  # ~3 iterations
    await scheduler.stop()
    assert orch.cleanup_stale_pending_ledger.call_count >= 2


@pytest.mark.asyncio
async def test_scheduler_skips_tick_when_manual_holds_lock(sm):
    """If _tick_running=True, scheduler won't fire until released."""
    orch = _mk_orch(sm)
    # Force a scheduled tick to be due.
    from csm.models.sync_config import SyncConfig
    async with sm() as db:
        cfg = SyncConfig(
            module="memory", enrolled_agents=["claude"],
            poll_interval_sec=30, enabled=True,
            updated_at=now_utc_naive(),
        )
        db.add(cfg)
        await db.commit()
        from sqlalchemy import text
        await db.execute(text(
            "UPDATE sync_config SET sync_mode='agent', "
            "tick_interval_hours=24 WHERE id = :i"
        ).bindparams(i=cfg.id))
        await db.commit()
    # Simulate manual tick in progress.
    assert orch.try_acquire_tick() is True

    # Wrap run_tick so we can detect if scheduler tried to fire.
    orch.run_tick = AsyncMock(return_value=None)

    scheduler = SyncTickScheduler(orch, sm, loop_sleep_sec=0.05)
    await scheduler.start()
    await asyncio.sleep(0.15)
    await scheduler.stop()
    # Manual is holding the lock → scheduler never got to call run_tick.
    orch.run_tick.assert_not_called()
    orch.release_tick()


# ---------------------------------------------------------------------------
# _should_tick_now — minute-level cadence (P1-2)
# ---------------------------------------------------------------------------


async def test_should_tick_minutes_wins_over_hours(sm):
    from csm.models.sync_agent_run import SyncAgentRun
    from csm.models.sync_config import SyncConfig
    sched = SyncTickScheduler(_mk_orch(sm), sm)
    async with sm() as db:
        db.add(SyncConfig(module="memory", enrolled_agents=["claude"],
                          poll_interval_sec=30, enabled=True,
                          sync_mode="agent", tick_interval_hours=6,
                          tick_interval_minutes=2))
        db.add(SyncAgentRun(ts=now_utc_naive() - timedelta(minutes=3),
                            trigger="scheduled", prompt_hash="",
                            input_state_hash="", input_snapshot_json={},
                            phase="done"))
        await db.commit()
    # 2-min cadence, last scheduled run 3 min ago → DUE.
    # (A 6h cadence from tick_interval_hours would say NOT due.)
    assert await sched._should_tick_now() is True


async def test_should_tick_minutes_not_yet_due(sm):
    from csm.models.sync_agent_run import SyncAgentRun
    from csm.models.sync_config import SyncConfig
    sched = SyncTickScheduler(_mk_orch(sm), sm)
    async with sm() as db:
        db.add(SyncConfig(module="memory", enrolled_agents=["claude"],
                          poll_interval_sec=30, enabled=True,
                          sync_mode="agent", tick_interval_hours=0,
                          tick_interval_minutes=5))
        db.add(SyncAgentRun(ts=now_utc_naive() - timedelta(minutes=1),
                            trigger="scheduled", prompt_hash="",
                            input_state_hash="", input_snapshot_json={},
                            phase="done"))
        await db.commit()
    assert await sched._should_tick_now() is False


async def test_should_tick_ignores_sync_mode(sm):
    # v1 `lock` mode is retired: sync_mode no longer gates scheduling. A module
    # with a positive interval fires regardless of the (vestigial) sync_mode.
    from csm.models.sync_config import SyncConfig
    sched = SyncTickScheduler(_mk_orch(sm), sm)
    async with sm() as db:
        db.add(SyncConfig(module="memory", enrolled_agents=["claude"],
                          poll_interval_sec=30, enabled=True,
                          sync_mode="lock", tick_interval_hours=0,
                          tick_interval_minutes=2))
        await db.commit()
    # never ticked + has an interval → due now, even though mode is "lock".
    assert await sched._should_tick_now() is True


async def test_should_tick_manual_only_when_both_zero(sm):
    from csm.models.sync_config import SyncConfig
    sched = SyncTickScheduler(_mk_orch(sm), sm)
    async with sm() as db:
        db.add(SyncConfig(module="memory", enrolled_agents=["claude"],
                          poll_interval_sec=30, enabled=True,
                          sync_mode="agent", tick_interval_hours=0,
                          tick_interval_minutes=0))
        await db.commit()
    assert await sched._should_tick_now() is False

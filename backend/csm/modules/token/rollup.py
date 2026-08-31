"""Hourly rollup + raw event TTL background worker (F7).

Owns two periodic jobs:
- rollup_tick(): every `rollup_interval_sec` (default 1h), aggregate `raw_token_event`
  rows older than the current hour into `hourly_rollup` (idempotent upsert by
  unique (bucket_hour, model, project_path)).
- ttl_tick(): every `ttl_interval_sec` (default 1h), delete `raw_token_event` rows
  whose ts is older than the retention window. The window is **runtime-managed**:
  it's read fresh from `user_preference.raw_event_retention_days` on every tick,
  so PUT /api/preferences retunes it without a restart. The `retention_days`
  constructor arg is only a fallback for when that row can't be read (e.g. a bare
  test DB). **A window of 0 disables TTL entirely** — every raw row is kept
  forever. rollup still runs regardless, so trend queries keep full history via
  hourly aggregates even after raw rows are pruned.

Order matters: rollup before TTL on each tick, so we never delete a raw row that
hasn't been counted yet.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import HourlyRollup, RawTokenEvent, UserPreference
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)


class RollupWorker:
    """Periodic rollup + TTL background loop.

    Single asyncio task; `start()` schedules it, `stop()` cancels.
    Idempotent: re-running on the same hour bucket just upserts the same numbers.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        tick_interval_sec: float = 3600.0,
        retention_days: int = 0,
    ) -> None:
        self._sm = sessionmaker
        self._interval = tick_interval_sec
        # Fallback retention window (days). The live value is read from
        # user_preference.raw_event_retention_days on every tick; this is only
        # used when that row can't be read (bare test DB, pre-seed boot).
        # 0 → keep raw_token_event rows forever.
        self._fallback_retention_days = int(retention_days)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="csm-rollup-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        # Run once immediately so a fresh deploy has rollup data within seconds.
        try:
            await self.run_once()
        except Exception:
            log.exception("rollup initial tick failed")
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            except TimeoutError:
                try:
                    await self.run_once()
                except Exception:
                    log.exception("rollup tick failed")
            else:
                break

    async def run_once(self) -> dict[str, int]:
        """Returns {'rolled_buckets': N, 'deleted_raw': N} for observability."""
        rolled = await self._rollup()
        deleted = await self._ttl()
        return {"rolled_buckets": rolled, "deleted_raw": deleted}

    async def _rollup(self) -> int:
        """Aggregate raw events into hourly_rollup. Only buckets strictly older than
        the current hour are rolled (current hour is still being written to)."""
        now = datetime.now(UTC).replace(tzinfo=None)
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        # SQLite-portable hour bucket: strftime returns 'YYYY-MM-DD HH:00:00' string.
        # We turn it back into datetime via datetime.strptime on the read side.
        bucket_expr = func.strftime("%Y-%m-%d %H:00:00", RawTokenEvent.ts)
        stmt = (
            select(
                bucket_expr.label("bucket"),
                RawTokenEvent.model,
                RawTokenEvent.project_path,
                RawTokenEvent.agent,     # M12: carry agent into rollup
                func.count(RawTokenEvent.id),
                func.coalesce(func.sum(RawTokenEvent.input_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.cache_creation_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.output_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.estimated_cost_usd), 0.0),
            )
            .where(RawTokenEvent.ts < current_hour)
            .group_by(
                "bucket",
                RawTokenEvent.model,
                RawTokenEvent.project_path,
                RawTokenEvent.agent,
            )
        )
        # Read once (read txn is short).
        async with self._sm() as db:
            rows = (await db.execute(stmt)).all()
        # Then upsert in small batches, each its own write txn, so the SQLite
        # writer slot is released between batches. Long single-txn loops here
        # were holding the WAL writer for many seconds and starving concurrent
        # writers (agent send_message, notifications, etc).
        BATCH = 100
        total = 0
        for i in range(0, len(rows), BATCH):
            chunk = rows[i : i + BATCH]
            async with self._sm() as db:
                for bucket_str, model, proj, agent, msg_c, in_t, cc, cr, out, cost in chunk:
                    bucket_dt = datetime.strptime(bucket_str, "%Y-%m-%d %H:%M:%S")
                    # SQLite unique indexes treat NULL values as distinct, so
                    # ON CONFLICT never fired for rows with a missing model or
                    # project and every hourly tick inserted another copy.
                    # Delete the exact logical key first (NULL-safe), then add
                    # one canonical row. This also repairs existing duplicates
                    # on the next worker tick.
                    await db.execute(
                        delete(HourlyRollup).where(
                            HourlyRollup.bucket_hour == bucket_dt,
                            HourlyRollup.model.is_(None)
                            if model is None else HourlyRollup.model == model,
                            HourlyRollup.project_path.is_(None)
                            if proj is None else HourlyRollup.project_path == proj,
                            HourlyRollup.agent == agent,
                        )
                    )
                    db.add(HourlyRollup(
                        bucket_hour=bucket_dt,
                        model=model,
                        project_path=proj,
                        agent=agent,     # M12
                        input_tokens=int(in_t),
                        cache_creation_tokens=int(cc),
                        cache_read_tokens=int(cr),
                        output_tokens=int(out),
                        estimated_cost_usd=float(cost),
                        msg_count=int(msg_c),
                        updated_at=now_utc_naive(),
                    ))
                await db.commit()
            total += len(chunk)
        return total

    async def _effective_retention_days(self) -> int:
        """Runtime-managed retention window, read fresh each tick.

        Source of truth is `user_preference.raw_event_retention_days`; falls
        back to the constructor default when the singleton row is missing or
        unreadable (a bare test DB has the table but no row). This is what makes
        the window adjustable via PUT /api/preferences without a restart."""
        try:
            async with self._sm() as db:
                pref = await db.get(UserPreference, 1)
            if pref is not None and pref.raw_event_retention_days is not None:
                return int(pref.raw_event_retention_days)
        except Exception:
            log.exception("rollup: could not read retention pref; using fallback")
        return self._fallback_retention_days

    async def _ttl(self) -> int:
        """Delete raw events older than the retention window. Returns rows
        affected. A window <= 0 means "keep everything" — skip the delete."""
        days = await self._effective_retention_days()
        if days <= 0:
            return 0
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        async with self._sm() as db:
            stmt = delete(RawTokenEvent).where(RawTokenEvent.ts < cutoff)
            res = await db.execute(stmt)
            await db.commit()
            return int(res.rowcount or 0)

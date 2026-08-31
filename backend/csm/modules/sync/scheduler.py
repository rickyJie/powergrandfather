"""SyncTickScheduler — background 60s loop that fires the SyncOrchestrator.

Reads `sync_config` for the enabled modules' `tick_interval_hours`,
decides whether to fire, and shares the same tick lock as the manual
`/api/sync/agent-tick` endpoint so concurrent triggers never overlap.

Between-tick behavior:

- `_stop_event.wait()` with a 60s timeout gives the loop a natural
  cadence AND lets `stop()` shut it down promptly without cancel spam.
- Every iteration, before deciding to fire, we call
  `cleanup_stale_pending_ledger()` which sweeps any `pending` ledger
  rows older than 30 days to `failed_terminal` (design v7 §2.1). This
  is cheap (single UPDATE, no adapter I/O).
- If a manual tick is in progress, we just skip this iteration —
  scheduling is best-effort, no queueing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models.sync_agent_run import SyncAgentRun
from csm.models.sync_config import SyncConfig
from csm.utils.time import now_utc_naive

if TYPE_CHECKING:
    from csm.modules.sync.orchestrator import SyncOrchestrator

log = logging.getLogger(__name__)


_DEFAULT_LOOP_SLEEP_SEC = 60.0


class SyncTickScheduler:
    def __init__(
        self,
        orchestrator: SyncOrchestrator,
        sessionmaker: async_sessionmaker,
        loop_sleep_sec: float = _DEFAULT_LOOP_SLEEP_SEC,
    ) -> None:
        self._orch = orchestrator
        self._sm = sessionmaker
        self._loop_sleep_sec = loop_sleep_sec
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            # Reset stop event in case orchestrator was stopped + restarted.
            self._orch._stop_event.clear()
            self._task = asyncio.create_task(
                self._loop(),
                name="sync-tick-scheduler",
            )

    async def stop(self) -> None:
        self._orch._stop_event.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=30.0)
        except TimeoutError:
            log.warning(
                "sync scheduler did not drain in 30s; cancelling task",
            )
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        finally:
            self._task = None

    async def _loop(self) -> None:
        while not self._orch._stop_event.is_set():
            try:
                # v7 §2.1: sweep old pending rows every tick regardless.
                try:
                    await self._orch.cleanup_stale_pending_ledger()
                except Exception:
                    log.exception(
                        "cleanup_stale_pending_ledger raised; continuing",
                    )

                fire = await self._should_tick_now()
                if fire and self._orch.try_acquire_tick():
                    try:
                        log.info(
                            "sync scheduler firing tick (trigger=scheduled)",
                        )
                        await self._orch.run_tick(trigger="scheduled")
                    except Exception:
                        log.exception(
                            "run_tick raised inside scheduler loop; "
                            "continuing so we don't lose the timer",
                        )
                        # run_tick's `finally` releases the tick lock.

                try:
                    await asyncio.wait_for(
                        self._orch._stop_event.wait(),
                        timeout=self._loop_sleep_sec,
                    )
                    # stop() called → clean exit.
                    break
                except TimeoutError:
                    # Normal wake-up — continue the loop.
                    continue

            except Exception:
                # Belt-and-suspenders: NOTHING escapes the loop except
                # stop_event → clean exit. Cancels raise
                # asyncio.CancelledError which bubbles through the
                # bare-except; that's fine.
                log.exception(
                    "sync scheduler loop caught unexpected exception; sleeping and continuing",
                )
                try:
                    await asyncio.wait_for(
                        self._orch._stop_event.wait(),
                        timeout=self._loop_sleep_sec,
                    )
                    break
                except TimeoutError:
                    continue

    async def _should_tick_now(self) -> bool:
        """Return True iff at least one enabled agent-mode module is due.

        Effective cadence per module: `tick_interval_minutes` when > 0,
        otherwise `tick_interval_hours * 60`. Modules with neither set
        (both 0) never opt into scheduled ticks. Cadence is clamped to
        >= 1 minute so a stray small value can't busy-spin the 60s loop.
        Due iff the last `trigger='scheduled'` run is older than the
        smallest active cadence.
        """
        async with self._sm() as db:
            configs = (await db.execute(select(SyncConfig))).scalars().all()
            # Every enabled module opts into scheduled ticks. (The old `lock`
            # v1 mode is retired — sync is agent-driven only now, so `sync_mode`
            # is no longer consulted here.)
            cadences_min: list[int] = []
            for c in configs:
                if not c.enabled:
                    continue
                minutes = getattr(c, "tick_interval_minutes", 0) or 0
                hours = getattr(c, "tick_interval_hours", 0) or 0
                eff = minutes if minutes > 0 else hours * 60
                if eff > 0:
                    cadences_min.append(max(1, eff))
        if not cadences_min:
            return False

        smallest = min(cadences_min)
        async with self._sm() as db:
            last = (
                await db.execute(
                    select(SyncAgentRun)
                    .where(
                        SyncAgentRun.trigger == "scheduled",
                    )
                    .order_by(SyncAgentRun.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        if last is None:
            return True
        due_at = last.ts + timedelta(minutes=smallest)
        return now_utc_naive() >= due_at


__all__ = ["SyncTickScheduler"]

"""Human-side heartbeat receiver.

Frontend `useWorktimeHeartbeat` composable POSTs to `/api/worktime/heartbeat`
every 30s while the tab is visible AND has seen a mouse/keyboard event in
the last 120s. Absence of a heartbeat past the 60s grace window closes the
current `kind=human` interval.

Materialization policy:

- One in-memory `_open_row_id` + `_last_seen_ts` pair guards the current
  open interval. All writes go through the sessionmaker.
- The 30s sweeper task exists so that a user who closes their last tab
  and never returns doesn't leave the row dangling in DB — it's not
  needed for correct `/live` numbers (those are clamped) but keeps the
  DB tidy and makes daily/weekly queries straightforward.
- Server reboot: `WorktimeService.reap_orphans_on_boot` closes any row
  that survived a crash with `source=reap`, capped to 30-min after start.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import WorkInterval, WorkIntervalKind, WorkIntervalSource
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)

_HEARTBEAT_GRACE_SEC = 60
_SWEEP_INTERVAL_SEC = 30
_HUMAN_INTERVAL_CAP_SEC = 24 * 60 * 60  # 24h absolute upper bound per row


class HeartbeatManager:
    """Owns the single active `kind=human` interval."""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sm = sessionmaker
        self._open_row_id: str | None = None
        self._open_start_ts: datetime | None = None
        self._last_seen_ts: datetime | None = None
        self._lock = asyncio.Lock()
        self._sweep_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        # No boot-time recovery needed here — WorktimeService.reap_orphans
        # runs before us at lifespan startup and closes any prior-run row.
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
        # Best-effort close of the currently-open interval on shutdown so
        # the row doesn't rely on the next-boot reap.
        async with self._lock:
            if self._open_row_id:
                await self._close_locked(end_ts=self._last_seen_ts or now_utc_naive())

    async def heartbeat(self) -> dict[str, object]:
        """Record a fresh heartbeat, opening or extending the interval.

        Returns a small status dict for the frontend to reflect state.
        """
        now = now_utc_naive()
        async with self._lock:
            reopened = False
            if self._open_row_id is None:
                await self._open_locked(start_ts=now)
            else:
                # Existing open interval — decide extend vs restart.
                gap_sec = (now - (self._last_seen_ts or now)).total_seconds()
                if gap_sec > _HEARTBEAT_GRACE_SEC:
                    # Grace lapsed: close the old row, open a fresh one.
                    await self._close_locked(end_ts=self._last_seen_ts or now)
                    await self._open_locked(start_ts=now)
                    reopened = True
                # Otherwise: same interval continues (no DB write).
                # 24h absolute cap: force a rollover so no single row grows
                # past a day.
                elif (
                    self._open_start_ts is not None
                    and (now - self._open_start_ts).total_seconds() >= _HUMAN_INTERVAL_CAP_SEC
                ):
                    await self._close_locked(end_ts=now)
                    await self._open_locked(start_ts=now)
                    reopened = True
            self._last_seen_ts = now
            return {
                "open_row_id": self._open_row_id,
                "reopened": reopened,
                "last_seen_ts": now.isoformat(),
            }

    async def _open_locked(self, start_ts: datetime) -> None:
        async with self._sm() as db:
            row = WorkInterval(
                kind=WorkIntervalKind.HUMAN,
                session_id=None,
                start_ts=start_ts,
                end_ts=None,
                source=WorkIntervalSource.HEARTBEAT,
            )
            db.add(row)
            await db.commit()
            self._open_row_id = row.id
            self._open_start_ts = start_ts

    async def _close_locked(self, end_ts: datetime) -> None:
        row_id = self._open_row_id
        if not row_id:
            return
        async with self._sm() as db:
            row = await db.get(WorkInterval, row_id)
            if row is None or row.end_ts is not None:
                self._open_row_id = None
                return
            capped_end = row.start_ts + timedelta(seconds=_HUMAN_INTERVAL_CAP_SEC)
            if end_ts > capped_end:
                end_ts = capped_end
            if end_ts < row.start_ts:
                end_ts = row.start_ts
            row.end_ts = end_ts
            await db.commit()
        self._open_row_id = None
        self._open_start_ts = None

    async def _sweep_loop(self) -> None:
        """Every 30s, close the row if the last heartbeat is past grace."""
        while True:
            try:
                await asyncio.sleep(_SWEEP_INTERVAL_SEC)
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worktime heartbeat sweep failed")

    async def _sweep_once(self) -> None:
        now = now_utc_naive()
        async with self._lock:
            if self._open_row_id is None or self._last_seen_ts is None:
                return
            if (now - self._last_seen_ts).total_seconds() > _HEARTBEAT_GRACE_SEC:
                await self._close_locked(end_ts=self._last_seen_ts)

    # ---- introspection ----

    def open_row_id(self) -> str | None:
        return self._open_row_id

    def last_seen_ts(self) -> datetime | None:
        return self._last_seen_ts


__all__ = ["HeartbeatManager"]

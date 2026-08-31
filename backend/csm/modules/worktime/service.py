"""Read-side aggregations + boot-time orphan reap.

Called by:
- `main.lifespan` at startup, via `reap_orphans_on_boot`.
- `GET /api/worktime/live`, via `live_totals`.

Live-totals math (design choice 3=a wall-clock accumulation):
overlapping intervals across sessions each count fully — no
union-collapsing. A straightforward SUM(clamped duration) per kind
suffices.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Integer, case, cast, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import WorkInterval, WorkIntervalKind, WorkIntervalSource
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)

_AGENT_INTERVAL_CAP_SEC = 30 * 60
_HUMAN_INTERVAL_CAP_SEC = 24 * 60 * 60
# Live-totals cache TTL. The widget polls /live every 5s and every open
# browser tab / mobile client polls independently, so without this each poll
# from each client triggers a fresh aggregation. A sub-poll-interval TTL
# collapses concurrent polls into one computation; staleness <= TTL is
# invisible on a 5s-polling, client-side-ticking widget.
_LIVE_CACHE_TTL_SEC = 1.0


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class LiveTotals:
    today_human_sec: int
    today_agent_sec: int
    all_human_sec: int
    all_agent_sec: int
    open_agent_sec: int
    open_human_sec: int
    open_agent_count: int
    day_bucket_utc: str  # ISO date of the UTC "today" the totals cover


class WorktimeService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sm = sessionmaker
        self._live_cache: LiveTotals | None = None
        self._live_cache_mono: float = 0.0
        self._live_lock = asyncio.Lock()

    async def reap_orphans_on_boot(self) -> int:
        """Close any interval left dangling by an unclean shutdown.

        Applies each kind's safety cap (agent=30min, human=24h) so a row
        from days ago doesn't get materialized as a huge chunk.
        Returns the number of rows closed.
        """
        now = now_utc_naive()
        closed = 0
        async with self._sm() as db:
            rows = (
                await db.execute(
                    select(WorkInterval).where(WorkInterval.end_ts.is_(None))
                )
            ).scalars().all()
            for row in rows:
                cap_sec = (
                    _AGENT_INTERVAL_CAP_SEC
                    if row.kind == WorkIntervalKind.AGENT
                    else _HUMAN_INTERVAL_CAP_SEC
                )
                cap_end = row.start_ts + timedelta(seconds=cap_sec)
                end_ts = min(now, cap_end)
                if end_ts < row.start_ts:
                    end_ts = row.start_ts
                row.end_ts = end_ts
                row.source = WorkIntervalSource.REAP
                closed += 1
            if closed:
                await db.commit()
        if closed:
            log.info("worktime reap closed %d orphan intervals on boot", closed)
        return closed

    async def live_totals(self) -> LiveTotals:
        """Cached read of the live worktime totals.

        Serves a value at most `_LIVE_CACHE_TTL_SEC` old so a burst of
        concurrent `/live` polls (multiple tabs / clients) collapses to a
        single aggregation instead of one full pass per poll. The heavy
        lifting lives in `_compute_live_totals`.
        """
        mono = time.monotonic()
        cached = self._live_cache
        if cached is not None and mono - self._live_cache_mono < _LIVE_CACHE_TTL_SEC:
            return cached
        async with self._live_lock:
            # Double-check: a peer coroutine may have refreshed while we waited.
            mono = time.monotonic()
            if (
                self._live_cache is not None
                and mono - self._live_cache_mono < _LIVE_CACHE_TTL_SEC
            ):
                return self._live_cache
            totals = await self._compute_live_totals()
            self._live_cache = totals
            self._live_cache_mono = time.monotonic()
            return totals

    async def _compute_live_totals(self) -> LiveTotals:
        now = now_utc_naive()
        day_start = _utc_day_start(now)

        async with self._sm() as db:
            # --- Closed intervals: aggregate in SQL. `work_interval` grows
            # unbounded (~10⁴/year assumption was already blown — 24k+ rows in
            # practice), and this ran on every /live poll from every client.
            # Materializing every row into Python objects per poll saturated
            # the single event loop. Closed rows never change, so a grouped
            # SUM keeps this O(1) in Python regardless of table size.
            #
            # `end_ts` is clamped to [start, start+cap] on write, but we
            # re-apply the cap in SQL so a legacy/unclamped row can't
            # over-count. julianday() → fractional days; ×86400 → seconds.
            # julianday() carries sub-second float error (a whole-second span
            # can come back as 29.9999…s), so we round to the nearest second
            # per row rather than truncate — whole-second data stays exact,
            # genuinely-fractional spans differ by <=0.5s (irrelevant here). ---
            start_jd = func.julianday(WorkInterval.start_ts)
            end_jd = func.julianday(WorkInterval.end_ts)
            cap_days = case(
                (
                    WorkInterval.kind == WorkIntervalKind.AGENT,
                    _AGENT_INTERVAL_CAP_SEC / 86400.0,
                ),
                else_=_HUMAN_INTERVAL_CAP_SEC / 86400.0,
            )
            eff_end_jd = func.min(end_jd, start_jd + cap_days)
            day_start_jd = func.julianday(day_start)
            all_sec = cast(func.round((eff_end_jd - start_jd) * 86400.0), Integer)
            today_sec = cast(
                func.round((eff_end_jd - func.max(start_jd, day_start_jd)) * 86400.0), Integer
            )
            stmt = (
                select(
                    WorkInterval.kind,
                    func.coalesce(func.sum(case((all_sec > 0, all_sec), else_=0)), 0),
                    func.coalesce(func.sum(case((today_sec > 0, today_sec), else_=0)), 0),
                )
                .where(WorkInterval.end_ts.is_not(None))
                .group_by(WorkInterval.kind)
            )
            all_agent_sec = today_agent_sec = 0
            all_human_sec = today_human_sec = 0
            for kind, all_s, today_s in (await db.execute(stmt)).all():
                if kind == WorkIntervalKind.AGENT:
                    all_agent_sec, today_agent_sec = int(all_s or 0), int(today_s or 0)
                else:
                    all_human_sec, today_human_sec = int(all_s or 0), int(today_s or 0)

            # --- Open intervals (end_ts IS NULL): normally a handful. Clamp to
            # `now` in Python exactly as before so the live ticker matches, and
            # accumulate the open-only totals the widget shows. Open rows clamp
            # from their actual start_ts (not day_start) so the trailing ● timer
            # doesn't jump at UTC midnight. ---
            open_agent_sec = 0
            open_human_sec = 0
            open_agent_count = 0
            open_rows = (
                await db.execute(
                    select(WorkInterval).where(WorkInterval.end_ts.is_(None))
                )
            ).scalars().all()
            for row in open_rows:
                cap_sec = (
                    _AGENT_INTERVAL_CAP_SEC
                    if row.kind == WorkIntervalKind.AGENT
                    else _HUMAN_INTERVAL_CAP_SEC
                )
                cap_end = row.start_ts + timedelta(seconds=cap_sec)
                row_end = min(now, cap_end)
                if row_end <= row.start_ts:
                    continue
                all_dur = int((row_end - row.start_ts).total_seconds())
                today_start = max(row.start_ts, day_start)
                today_dur = int((row_end - today_start).total_seconds())
                if row.kind == WorkIntervalKind.AGENT:
                    all_agent_sec += all_dur
                    if today_dur > 0:
                        today_agent_sec += today_dur
                    open_agent_sec += all_dur
                    open_agent_count += 1
                else:
                    all_human_sec += all_dur
                    if today_dur > 0:
                        today_human_sec += today_dur
                    open_human_sec += all_dur

        return LiveTotals(
            today_human_sec=today_human_sec,
            today_agent_sec=today_agent_sec,
            all_human_sec=all_human_sec,
            all_agent_sec=all_agent_sec,
            open_agent_sec=open_agent_sec,
            open_human_sec=open_human_sec,
            open_agent_count=open_agent_count,
            day_bucket_utc=day_start.date().isoformat(),
        )


__all__ = ["LiveTotals", "WorktimeService"]

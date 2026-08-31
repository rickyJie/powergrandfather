"""Agent-side worktime tracker.

Subscribes to `MESSAGE_USER_SENT`, `MESSAGE_ASSISTANT_DONE`,
`SESSION_ENDED`, `SESSION_CRASHED` and maintains a per-session interval
state machine that writes rows to `work_interval` with `kind=agent`.

State machine (per external session id):

    idle ──USER_SENT──▶ open ──ASSISTANT_DONE──▶ idle
              │                    ▲
              │                    │
              └── USER_SENT (coalesce: close previous, open fresh) ──┘

    open ──SESSION_ENDED / SESSION_CRASHED──▶ idle (close at event.ts)
    open ──60min safety cap on next tick──▶ idle (close at start+30min)

The 30-min cap protects against a lost ASSISTANT_DONE (e.g. process crash
between the two events) inflating a row into an hours-long interval.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import Session, WorkInterval, WorkIntervalKind, WorkIntervalSource
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)

# Safety cap on a single agent interval. Any USER_SENT → ASSISTANT_DONE
# span longer than this is capped rather than persisted verbatim, because
# the excess almost always means we missed a close event.
_MAX_INTERVAL_SEC = 30 * 60
# Sweeper cadence — cheap SELECT + UPDATE against `end_ts IS NULL` rows.
_SWEEP_INTERVAL_SEC = 60


def _to_naive(dt: datetime) -> datetime:
    """Strip tzinfo — DB columns are naive UTC (see utils/time.py)."""
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


class WorktimeTracker:
    """Owns per-session state; writes `kind=agent` rows to work_interval."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        event_stream: EventStream,
    ) -> None:
        self._sm = sessionmaker
        self._es = event_stream
        self._sub_id: str | None = None
        # external_session_id -> row_id of open work_interval
        self._open: dict[str, str] = {}
        self._sweep_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._sub_id = self._es.subscribe(
            [
                EventType.MESSAGE_USER_SENT,
                EventType.MESSAGE_ASSISTANT_DONE,
                EventType.SESSION_INTERRUPTED,
                EventType.SESSION_ENDED,
                EventType.SESSION_CRASHED,
            ],
            self._on_event,
        )
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None
        if self._sub_id:
            self._es.unsubscribe(self._sub_id)
            self._sub_id = None

    async def _on_event(self, event: Event) -> None:
        try:
            if event.type == EventType.MESSAGE_USER_SENT:
                await self._open_interval(event)
            elif event.type in (
                EventType.MESSAGE_ASSISTANT_DONE,
                EventType.SESSION_INTERRUPTED,
            ):
                # Interrupt ends the turn just like a normal assistant-done —
                # close (non-terminal) so a fresh USER_SENT opens a new one.
                await self._close_interval(event)
            elif event.type in (EventType.SESSION_ENDED, EventType.SESSION_CRASHED):
                await self._close_interval(event, terminal=True)
        except Exception:
            # EventStream contract: never break the stream.
            log.exception("worktime tracker failed on event type=%s", event.type)

    async def _resolve_csm_session_id(self, external_sid: str) -> str | None:
        async with self._sm() as db:
            row = (
                await db.execute(
                    select(Session.id)
                    .where(
                        Session.external_session_id == external_sid,
                        Session.superseded_by.is_(None),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row

    async def _open_interval(self, event: Event) -> None:
        external_sid = event.session_id
        if not external_sid:
            return
        start_ts = _to_naive(event.ts)
        # Coalesce: if we already have an open interval for this session
        # (two USER_SENT without an intervening ASSISTANT_DONE), close the
        # previous one at the new start so the new one takes over cleanly.
        if external_sid in self._open:
            await self._materialize_close(
                row_id=self._open.pop(external_sid), end_ts=start_ts
            )

        csm_sid = await self._resolve_csm_session_id(external_sid)
        # `session_id` on work_interval refers to CSM row id. Persist even
        # when we can't resolve (manual_external session) — the row is
        # still useful for global totals; session_id just stays NULL.
        async with self._sm() as db:
            row = WorkInterval(
                kind=WorkIntervalKind.AGENT,
                session_id=csm_sid,
                start_ts=start_ts,
                end_ts=None,
                source=WorkIntervalSource.EVENT,
            )
            db.add(row)
            await db.commit()
            self._open[external_sid] = row.id

    async def _close_interval(self, event: Event, terminal: bool = False) -> None:
        external_sid = event.session_id
        if not external_sid:
            return
        row_id = self._open.pop(external_sid, None)
        if row_id is None:
            # ASSISTANT_DONE with no matching open (e.g. we started up
            # mid-stream, or the USER_SENT arrived before we subscribed).
            # Nothing to close.
            return
        end_ts = _to_naive(event.ts)
        await self._materialize_close(row_id=row_id, end_ts=end_ts)

    async def _materialize_close(self, row_id: str, end_ts: datetime) -> None:
        """Apply the 30-min cap and write end_ts back to the row."""
        async with self._sm() as db:
            row = await db.get(WorkInterval, row_id)
            if row is None or row.end_ts is not None:
                return
            capped_end = row.start_ts + timedelta(seconds=_MAX_INTERVAL_SEC)
            if end_ts > capped_end:
                end_ts = capped_end
            # Guard against a clock skew that would make end_ts < start_ts;
            # keep the interval a no-op (zero duration) rather than negative.
            if end_ts < row.start_ts:
                end_ts = row.start_ts
            row.end_ts = end_ts
            await db.commit()

    async def _sweep_loop(self) -> None:
        """Every 60s, close any agent row whose start is older than the cap.

        Guards against a lost `MESSAGE_ASSISTANT_DONE` (crashed claude
        process, JSONL never emitted the done marker, etc.) leaving a row
        `end_ts IS NULL` in DB — `live_totals` already clamps display, but
        the row stays dangling until next boot's reap without this loop.
        """
        while True:
            try:
                await asyncio.sleep(_SWEEP_INTERVAL_SEC)
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worktime tracker sweep failed")

    async def _sweep_once(self) -> None:
        now = now_utc_naive()
        threshold = now - timedelta(seconds=_MAX_INTERVAL_SEC)
        swept: set[str] = set()
        async with self._sm() as db:
            rows = (
                await db.execute(
                    select(WorkInterval).where(
                        WorkInterval.kind == WorkIntervalKind.AGENT,
                        WorkInterval.end_ts.is_(None),
                        WorkInterval.start_ts < threshold,
                    )
                )
            ).scalars().all()
            for row in rows:
                row.end_ts = row.start_ts + timedelta(seconds=_MAX_INTERVAL_SEC)
                row.source = WorkIntervalSource.REAP
                swept.add(row.id)
            if swept:
                await db.commit()
        if swept:
            # Prune internal map so a late DONE event doesn't try to close
            # a row we just reaped (the guard in _materialize_close would
            # no-op it anyway, but this keeps state honest).
            self._open = {k: v for k, v in self._open.items() if v not in swept}
            log.info("worktime tracker swept %d overdue agent intervals", len(swept))

    # ---- introspection helpers (used by tests + /live query) ----

    def open_external_session_ids(self) -> list[str]:
        return list(self._open.keys())

    def open_row_ids(self) -> list[str]:
        return list(self._open.values())


__all__ = ["WorktimeTracker"]

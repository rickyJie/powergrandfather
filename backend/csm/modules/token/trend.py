"""Token usage trend / window queries."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._serialize import iso_utc
from csm.models import HitObservation, HourlyRollup, RawTokenEvent
from csm.utils.time import now_utc_naive


class TrendQueries:
    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    async def current_window(
        self,
        hours: float = 5.0,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        end = now_utc_naive()
        start = end - timedelta(hours=hours)
        total_expr = (
            RawTokenEvent.input_tokens
            + RawTokenEvent.cache_creation_tokens
            + RawTokenEvent.cache_read_tokens
            + RawTokenEvent.output_tokens
        )
        async with self._sm() as db:
            stmt = (
                select(
                    func.count(RawTokenEvent.id),
                    func.coalesce(func.sum(RawTokenEvent.input_tokens), 0),
                    func.coalesce(func.sum(RawTokenEvent.cache_creation_tokens), 0),
                    func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0),
                    func.coalesce(func.sum(RawTokenEvent.output_tokens), 0),
                    func.coalesce(func.sum(RawTokenEvent.estimated_cost_usd), 0.0),
                )
                .where(RawTokenEvent.ts > start, RawTokenEvent.ts <= end)
            )
            stmt = self._apply_filters(stmt, filters)
            res = (await db.execute(stmt)).one()
            count, in_t, cc, cr, out, cost = res

            # Top session for anomaly-detection presets.
            top_stmt = (
                select(
                    RawTokenEvent.external_session_id,
                    func.coalesce(func.sum(total_expr), 0).label("session_total"),
                )
                .where(RawTokenEvent.ts > start, RawTokenEvent.ts <= end)
                .group_by(RawTokenEvent.external_session_id)
                .order_by(func.coalesce(func.sum(total_expr), 0).desc())
                .limit(1)
            )
            top_stmt = self._apply_filters(top_stmt, filters)
            top_row = (await db.execute(top_stmt)).first()

            # Claude-only aggregate for cache-hit-ratio checks. GLM / other
            # proxies don't emit prompt-cache tokens, so mixing them into
            # the aggregate ratio drags it toward 0. Cache-check presets
            # should read `cache_hit_ratio_claude` instead of the raw one.
            claude_stmt = (
                select(
                    func.coalesce(func.sum(RawTokenEvent.input_tokens), 0),
                    func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0),
                    func.coalesce(func.sum(RawTokenEvent.output_tokens), 0),
                    func.coalesce(func.sum(total_expr), 0),
                )
                .where(
                    RawTokenEvent.ts > start,
                    RawTokenEvent.ts <= end,
                    RawTokenEvent.model.like("claude%"),
                )
            )
            claude_stmt = self._apply_filters(claude_stmt, filters)
            claude_row = (await db.execute(claude_stmt)).one()

        total_tokens = int(in_t + cc + cr + out)
        top_id = (top_row[0] if top_row is not None else None) or None
        top_tokens = int(top_row[1]) if top_row is not None else 0
        top_share = (top_tokens / total_tokens) if total_tokens > 0 else 0.0
        # Cache-hit denominator excludes cache_creation (which is *writing* to
        # cache — new prompt content by definition can't be a hit). So the
        # ratio compares "reads that came from cache" vs "reads that had to
        # be re-processed (input) + generated (output)".
        cache_denom = int(cr) + int(in_t) + int(out)
        cache_hit_ratio = (int(cr) / cache_denom) if cache_denom > 0 else 0.0

        # Claude-only cache-hit metrics.
        cl_in, cl_cr, cl_out, cl_total = claude_row
        cl_denom = int(cl_in) + int(cl_cr) + int(cl_out)
        cache_hit_ratio_claude = (int(cl_cr) / cl_denom) if cl_denom > 0 else 0.0

        return {
            "window_hours": hours,
            "msg_count": int(count),
            "input_tokens": int(in_t),
            "cache_creation_tokens": int(cc),
            "cache_read_tokens": int(cr),
            "output_tokens": int(out),
            "total_tokens": total_tokens,
            "estimated_cost_usd": float(cost),
            "top_session_id": top_id,
            "top_session_tokens": top_tokens,
            "top_session_share": round(top_share, 4),   # 0.0-1.0
            "cache_hit_ratio": round(cache_hit_ratio, 4),  # 0.0-1.0 (all models)
            "cache_hit_ratio_claude": round(cache_hit_ratio_claude, 4),  # 0.0-1.0 (claude-* only)
            "claude_total_tokens": int(cl_total),
            "start_utc": iso_utc(start),
            "end_utc": iso_utc(end),
        }

    _FILTER_COLS = {
        "model": RawTokenEvent.model,
        "project": RawTokenEvent.project_path,
        "source": RawTokenEvent.source,
        "task": RawTokenEvent.task_name,
        "command_type": RawTokenEvent.command_type,
        "session": RawTokenEvent.external_session_id,
        # M12: agent-scoped view. Value is the adapter name (claude / codex /
        # gemini / ...). Frontend Tokens.vue defaults to the user's default
        # agent from UserPreference.default_agent. NULL rows (pre-M12 data
        # or manual_external that never resolved) match nothing — they're
        # visible only when `agent` isn't in the filter set.
        "agent": RawTokenEvent.agent,
    }

    _ROLLUP_FILTER_COLS = {
        "model": HourlyRollup.model,
        "project": HourlyRollup.project_path,
        "agent": HourlyRollup.agent,
    }

    def _apply_filters(self, stmt, filters: dict[str, Any] | None):
        """Add WHERE clauses for any of FILTER_COLS keys present in filters."""
        if not filters:
            return stmt
        for k, v in filters.items():
            if v in (None, "", []):
                continue
            col = self._FILTER_COLS.get(k)
            if col is None:
                continue
            if isinstance(v, list):
                stmt = stmt.where(col.in_(v))
            else:
                stmt = stmt.where(col == v)
        return stmt

    def _can_use_rollups(self, filters: dict[str, Any] | None) -> bool:
        """Whether hourly rollups can represent every active filter.

        Rollups retain only model, project and agent dimensions. Falling back
        to an unfiltered rollup for a source/session/task filter would leak
        unrelated usage into the result, so in that case raw data is the only
        accurate source.
        """
        if not filters:
            return True
        return not any(
            value not in (None, "", []) and key not in self._ROLLUP_FILTER_COLS
            for key, value in filters.items()
        )

    def _apply_rollup_filters(self, stmt, filters: dict[str, Any] | None):
        if not filters:
            return stmt
        for key, value in filters.items():
            if value in (None, "", []):
                continue
            col = self._ROLLUP_FILTER_COLS.get(key)
            if col is None:
                continue
            stmt = stmt.where(col.in_(value) if isinstance(value, list) else col == value)
        return stmt

    async def history(
        self,
        hours: int = 24,
        granularity: str = "hour",
        start: datetime | None = None,
        end: datetime | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return per-bucket sums for the last `hours` hours.

        For windows > 7 days we merge in HourlyRollup for buckets absent from
        raw data. Rollups are grouped to the requested granularity and receive
        every filter they can represent; if a filter uses a dimension rollups
        do not retain, they are skipped instead of leaking unrelated usage.

        ``start`` / ``end`` (UTC) take precedence over ``hours`` if supplied.
        ``filters`` accepts: model, project, source, task, command_type, session.
        Each value may be a single scalar or a list (treated as IN-clause).
        granularity: 'hour' (default) or 'day'.
        """
        if end is None:
            end = now_utc_naive().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if start is None:
            start = end - timedelta(hours=hours)
        # Granularity → strftime format. 'week' snaps to ISO week start (Monday).
        if granularity == "day":
            bucket_fmt = "%Y-%m-%d 00:00:00"
            bucket = func.strftime(bucket_fmt, RawTokenEvent.ts)
        elif granularity == "week":
            # ISO week label, e.g. "2026-W26".
            bucket = func.strftime("%Y-W%W", RawTokenEvent.ts)
        elif granularity == "minute":
            bucket = func.strftime("%Y-%m-%d %H:%M:00", RawTokenEvent.ts)
        elif granularity == "5min":
            # Snap to 5-minute floor via epoch modulo. (Plain `/300*300` is
            # unusable: SQLAlchemy compiles the integer division as float,
            # collapsing the bucket to per-minute resolution.)
            _epoch = func.cast(func.strftime("%s", RawTokenEvent.ts), Integer)
            _floor = _epoch.op("-")(_epoch.op("%")(300))
            bucket = func.strftime(
                "%Y-%m-%d %H:%M:00",
                func.datetime(_floor, "unixepoch"),
            )
        else:
            bucket_fmt = "%Y-%m-%d %H:00:00"
            bucket = func.strftime(bucket_fmt, RawTokenEvent.ts)
        # Re-derive hours so the rollup-merge below stays consistent.
        hours = max(1, int((end - start).total_seconds() // 3600))
        async with self._sm() as db:
            stmt = (
                select(
                    bucket.label("bucket"),
                    func.count(RawTokenEvent.id),
                    func.coalesce(func.sum(RawTokenEvent.input_tokens), 0),
                    func.coalesce(func.sum(RawTokenEvent.cache_creation_tokens), 0),
                    func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0),
                    func.coalesce(func.sum(RawTokenEvent.output_tokens), 0),
                    func.coalesce(func.sum(RawTokenEvent.estimated_cost_usd), 0.0),
                )
                .where(RawTokenEvent.ts >= start, RawTokenEvent.ts < end)
                .group_by("bucket")
                .order_by("bucket")
            )
            stmt = self._apply_filters(stmt, filters)
            raw_rows = (await db.execute(stmt)).all()
            by_bucket: dict[str, dict[str, Any]] = {
                str(r[0]): {
                    "bucket": str(r[0]),
                    "msg_count": int(r[1]),
                    "input_tokens": int(r[2]),
                    "cache_creation_tokens": int(r[3]),
                    "cache_read_tokens": int(r[4]),
                    "output_tokens": int(r[5]),
                    "estimated_cost_usd": float(r[6]),
                }
                for r in raw_rows
            }
            # For long windows, fill from HourlyRollup for buckets raw data no
            # longer has. The rollup bucket MUST use the same granularity as
            # the raw query; mixing daily raw buckets with hourly rollups both
            # duplicated usage and produced a malformed 30d chart.
            if (
                hours > 168
                and granularity in {"hour", "day", "week"}
                and self._can_use_rollups(filters)
            ):
                if granularity == "day":
                    rollup_bucket = func.strftime(
                        "%Y-%m-%d 00:00:00", HourlyRollup.bucket_hour
                    )
                elif granularity == "week":
                    rollup_bucket = func.strftime("%Y-W%W", HourlyRollup.bucket_hour)
                else:
                    rollup_bucket = func.strftime(
                        "%Y-%m-%d %H:00:00", HourlyRollup.bucket_hour
                    )
                stmt2 = (
                    select(
                        rollup_bucket.label("bucket"),
                        func.coalesce(func.sum(HourlyRollup.msg_count), 0),
                        func.coalesce(func.sum(HourlyRollup.input_tokens), 0),
                        func.coalesce(func.sum(HourlyRollup.cache_creation_tokens), 0),
                        func.coalesce(func.sum(HourlyRollup.cache_read_tokens), 0),
                        func.coalesce(func.sum(HourlyRollup.output_tokens), 0),
                        func.coalesce(func.sum(HourlyRollup.estimated_cost_usd), 0.0),
                    )
                    .where(
                        HourlyRollup.bucket_hour >= start,
                        HourlyRollup.bucket_hour < end,
                    )
                    .group_by("bucket")
                )
                stmt2 = self._apply_rollup_filters(stmt2, filters)
                for r in (await db.execute(stmt2)).all():
                    key = str(r[0])
                    if key not in by_bucket:  # raw takes precedence for recent data
                        by_bucket[key] = {
                            "bucket": key,
                            "msg_count": int(r[1]),
                            "input_tokens": int(r[2]),
                            "cache_creation_tokens": int(r[3]),
                            "cache_read_tokens": int(r[4]),
                            "output_tokens": int(r[5]),
                            "estimated_cost_usd": float(r[6]),
                        }
        return sorted(by_bucket.values(), key=lambda x: x["bucket"])

    async def data_range(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the accurate available range for the current filter scope.

        Raw rows and hourly rollups overlap. Counts therefore use raw rows plus
        only rollup rows strictly before the earliest matching raw event.
        """
        async with self._sm() as db:
            raw_stmt = select(
                func.min(RawTokenEvent.ts),
                func.max(RawTokenEvent.ts),
                func.count(RawTokenEvent.id),
            )
            raw_stmt = self._apply_filters(raw_stmt, filters)
            raw = (await db.execute(raw_stmt)).one()

            roll = (None, None, 0)
            if self._can_use_rollups(filters):
                roll_stmt = select(
                    func.min(HourlyRollup.bucket_hour),
                    func.max(HourlyRollup.bucket_hour),
                    func.coalesce(func.sum(HourlyRollup.msg_count), 0),
                )
                # Raw data is authoritative wherever it exists. Restrict the
                # rollup side to the non-overlapping historical prefix.
                if raw[0] is not None:
                    # A rollup timestamp is the *start* of its hour. Comparing
                    # it with an event at 05:46 would otherwise include the
                    # 05:00 rollup and count that event twice. Raw owns the
                    # entire hour containing its earliest row.
                    raw_hour = raw[0].replace(minute=0, second=0, microsecond=0)
                    roll_stmt = roll_stmt.where(HourlyRollup.bucket_hour < raw_hour)
                roll_stmt = self._apply_rollup_filters(roll_stmt, filters)
                roll = (await db.execute(roll_stmt)).one()

        def _min(a, b):
            if a is None:
                return b
            if b is None:
                return a
            return min(a, b)

        def _max(a, b):
            if a is None:
                return b
            if b is None:
                return a
            return max(a, b)

        return {
            "earliest": iso_utc(_min(raw[0], roll[0])),
            "latest": iso_utc(_max(raw[1], roll[1])),
            "event_count": int(raw[2] or 0) + int(roll[2] or 0),
        }

    _SCOPE_COLS = {
        "session": RawTokenEvent.external_session_id,
        "project": RawTokenEvent.project_path,
        "model": RawTokenEvent.model,
        "task": RawTokenEvent.task_name,
        "source": RawTokenEvent.source,
    }

    _SORT_EXPRS = {
        "cache_creation_tokens": func.sum(RawTokenEvent.cache_creation_tokens),
        "input_tokens": func.sum(RawTokenEvent.input_tokens),
        "cache_read_tokens": func.sum(RawTokenEvent.cache_read_tokens),
        "output_tokens": func.sum(RawTokenEvent.output_tokens),
        "total_tokens": (
            func.sum(RawTokenEvent.input_tokens)
            + func.sum(RawTokenEvent.cache_creation_tokens)
            + func.sum(RawTokenEvent.cache_read_tokens)
            + func.sum(RawTokenEvent.output_tokens)
        ),
        "cost": func.sum(RawTokenEvent.estimated_cost_usd),
        "msg_count": func.count(RawTokenEvent.id),
    }

    async def top_consumers(
        self,
        scope: str = "session",
        hours: int = 5,
        limit: int = 10,
        sort_by: str = "cache_creation_tokens",
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        start = now_utc_naive() - timedelta(hours=hours)
        if scope not in self._SCOPE_COLS:
            raise ValueError(f"unknown scope: {scope}")
        if sort_by not in self._SORT_EXPRS:
            raise ValueError(f"unknown sort_by: {sort_by}")
        key_col = self._SCOPE_COLS[scope]
        sort_expr = self._SORT_EXPRS[sort_by]
        # For session scope, attach the most-recent project_path so the UI can
        # show "what was running" without a second lookup. MAX() over a stable
        # string column is fine here — a single external_session_id corresponds
        # to a single jsonl file → a single cwd.
        attach_project = scope == "session"
        async with self._sm() as db:
            select_cols = [
                key_col.label("entity"),
                func.count(RawTokenEvent.id),
                func.coalesce(func.sum(RawTokenEvent.input_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.cache_creation_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.output_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.estimated_cost_usd), 0.0),
            ]
            if attach_project:
                select_cols.append(func.max(RawTokenEvent.project_path))
            stmt = (
                select(*select_cols)
                .where(RawTokenEvent.ts > start)
                .group_by(key_col)
                .order_by(sort_expr.desc())
                .limit(limit)
            )
            stmt = self._apply_filters(stmt, filters)
            rows = (await db.execute(stmt)).all()
            out: list[dict[str, Any]] = []
            for r in rows:
                item = {
                    "entity": r[0],
                    "msg_count": int(r[1]),
                    "input_tokens": int(r[2]),
                    "cache_creation_tokens": int(r[3]),
                    "cache_read_tokens": int(r[4]),
                    "output_tokens": int(r[5]),
                    "total_tokens": int(r[2] + r[3] + r[4] + r[5]),
                    "estimated_cost_usd": float(r[6]),
                }
                if attach_project:
                    item["project_path"] = r[7]
                out.append(item)
            return out

    async def facet_values(self, facet: str, hours: int = 24, limit: int = 50) -> list[str]:
        """Return distinct values seen for a given filter facet in the last N hours.

        Useful for populating filter dropdowns in the UI.
        """
        col = self._FILTER_COLS.get(facet)
        if col is None:
            raise ValueError(f"unknown facet: {facet}")
        start = now_utc_naive() - timedelta(hours=hours)
        async with self._sm() as db:
            stmt = (
                select(col)
                .where(RawTokenEvent.ts > start, col.is_not(None))
                .distinct()
                .limit(limit)
            )
            rows = (await db.execute(stmt)).all()
            return sorted({r[0] for r in rows if r[0]})

    async def export_rows(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 50000,
    ):
        """Yield raw_token_event rows for CSV export within optional time range / filters."""
        end = end or now_utc_naive()
        start = start or (end - timedelta(days=7))
        async with self._sm() as db:
            stmt = (
                select(
                    RawTokenEvent.ts,
                    RawTokenEvent.external_session_id,
                    RawTokenEvent.project_path,
                    RawTokenEvent.model,
                    RawTokenEvent.source,
                    RawTokenEvent.task_name,
                    RawTokenEvent.command_type,
                    RawTokenEvent.input_tokens,
                    RawTokenEvent.cache_creation_tokens,
                    RawTokenEvent.cache_read_tokens,
                    RawTokenEvent.output_tokens,
                    RawTokenEvent.estimated_cost_usd,
                )
                .where(RawTokenEvent.ts >= start, RawTokenEvent.ts < end)
                .order_by(RawTokenEvent.ts.desc())
                .limit(limit)
            )
            stmt = self._apply_filters(stmt, filters)
            rows = (await db.execute(stmt)).all()
            return rows

    async def hit_observations(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._sm() as db:
            stmt = (
                select(HitObservation)
                .order_by(HitObservation.ts.desc())
                .limit(limit)
            )
            rows = (await db.execute(stmt)).scalars().all()
            return [
                {
                    "id": r.id,
                    "ts": iso_utc(r.ts),
                    "reset_text": r.reset_text,
                    "msg_count_5h": r.msg_count_5h,
                    "cc_tokens_5h": r.cc_tokens_5h,
                    "cr_tokens_5h": r.cr_tokens_5h,
                    "input_tokens_5h": r.input_tokens_5h,
                    "output_tokens_5h": r.output_tokens_5h,
                }
                for r in rows
            ]
